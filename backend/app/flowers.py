"""Owner-scoped flowers earned from durable, deduplicated accomplishments."""
import json
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from app import auth
from app.identity import owner_id

FlowerSpecies = Literal['sunflower', 'daisy', 'tulip', 'cosmos', 'lavender', 'hydrangea']
FLOWERS = (
    ('sunflower', '向日葵', '初始花种，自由选择', None, 0),
    ('daisy', '雏菊', '初始花种，自由选择', None, 0),
    ('tulip', '郁金香', '初始花种，自由选择', None, 0),
    ('cosmos', '波斯菊', '完成第一个完整目标', 'goal', 1),
    ('lavender', '薰衣草', '第一次习惯阶段达标', 'habit', 1),
    ('hydrangea', '绣球花', '累计获得五份成果', 'total', 5),
)
BASE_SPECIES = frozenset(row[0] for row in FLOWERS if row[3] is None)
SCHEMA = '''
CREATE TABLE IF NOT EXISTS flower_achievements (
 id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('goal','habit')),
 achieved_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS flower_achievement_owner ON flower_achievements(owner_id);
CREATE TABLE IF NOT EXISTS flower_unlocks (
 owner_id TEXT NOT NULL, species TEXT NOT NULL CHECK(species IN ('cosmos','lavender','hydrangea')),
 unlocked_at TEXT NOT NULL, seen_at TEXT, PRIMARY KEY(owner_id,species));
'''


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def counts(db, owner):
    result = {'goal': 0, 'habit': 0}
    for row in db.execute('SELECT kind,count(*) AS n FROM flower_achievements WHERE owner_id=? GROUP BY kind', (owner,)):
        result[row['kind']] = row['n']
    return {**result, 'total': result['goal'] + result['habit']}


def award(db, owner):
    totals = counts(db, owner)
    for species, _name, _condition, metric, target in FLOWERS:
        if metric and totals[metric] >= target:
            db.execute('INSERT OR IGNORE INTO flower_unlocks VALUES (?,?,?,NULL)', (owner, species, timestamp()))


def record(db, owner, kind, source_id, achieved_at):
    # Source ids are stable across guest-to-account transfer and repeated completion.
    db.execute('INSERT OR IGNORE INTO flower_achievements VALUES (?,?,?,?)',
               (kind + ':' + source_id, owner, kind, achieved_at))
    award(db, owner)


def sync(db, owner):
    """Backfill only recorded accomplishments; never infer from having a plant."""
    goals = db.execute('''SELECT c.plant_id,c.confirmed_at FROM goal_collections c
        JOIN plants p ON p.id=c.plant_id AND p.owner_id=c.owner_id AND p.kind='goal'
        WHERE c.owner_id=? AND (
          EXISTS (SELECT 1 FROM completion_notices n WHERE n.owner_id=c.owner_id
                  AND n.plant_id=c.plant_id AND n.status='finished')
          OR NOT EXISTS (SELECT 1 FROM completion_notices n WHERE n.owner_id=c.owner_id
                         AND n.plant_id=c.plant_id))''', (owner,)).fetchall()
    for row in goals:
        record(db, owner, 'goal', row['plant_id'], row['confirmed_at'])
    ignored = 0
    habits = db.execute('''SELECT c.stage_id,c.snapshot_json,c.confirmed_at FROM habit_collections c
        JOIN plants p ON p.id=c.plant_id AND p.owner_id=c.owner_id AND p.kind='habit'
        JOIN habit_stages s ON s.id=c.stage_id AND s.owner_id=c.owner_id AND s.plant_id=c.plant_id
        WHERE c.owner_id=?''', (owner,)).fetchall()
    for row in habits:
        try:
            snap = json.loads(row['snapshot_json'])
            valid = (isinstance(snap, dict) and type(snap.get('threshold')) is int
                     and snap['threshold'] > 0 and type(snap.get('completed')) is int
                     and snap['completed'] >= snap['threshold'] and bool(row['confirmed_at']))
        except (ValueError, TypeError):
            valid = False
        if valid:
            record(db, owner, 'habit', row['stage_id'], row['confirmed_at'])
        else:
            ignored += 1
    award(db, owner)
    return ignored


def available_species(db, owner):
    sync(db, owner)
    return BASE_SPECIES | {r['species'] for r in db.execute('SELECT species FROM flower_unlocks WHERE owner_id=?', (owner,))}


def require_flower(db, owner, species):
    if species is not None and species not in available_species(db, owner):
        raise auth.AuthError('FLOWER_LOCKED', '这朵花还未解锁，请选择已拥有的花种。', 409)


def catalog(db, owner):
    ignored = sync(db, owner)
    totals = counts(db, owner)
    saved = {r['species']: dict(r) for r in db.execute('SELECT * FROM flower_unlocks WHERE owner_id=?', (owner,))}
    items = []
    for species, name, condition, metric, target in FLOWERS:
        grant = saved.get(species)
        items.append({'species': species, 'name': name, 'condition': condition,
                      'unlocked': metric is None or grant is not None,
                      'unlocked_at': grant['unlocked_at'] if grant else None,
                      'is_new': bool(grant and grant['seen_at'] is None),
                      'progress': min(totals[metric], target) if metric else 0, 'target': target})
    return {'flowers': items, 'counts': totals, 'unread_count': sum(i['is_new'] for i in items),
            'history_pending': ignored}


def transfer_unlocks(db, guest, account):
    # Existing account grants keep their read state; new guest grants retain theirs.
    for row in db.execute('SELECT * FROM flower_unlocks WHERE owner_id=?', (guest,)).fetchall():
        db.execute('''INSERT INTO flower_unlocks VALUES (?,?,?,?) ON CONFLICT(owner_id,species)
            DO UPDATE SET unlocked_at=MIN(flower_unlocks.unlocked_at,excluded.unlocked_at)''',
                   (account, row['species'], row['unlocked_at'], row['seen_at']))
    db.execute('DELETE FROM flower_unlocks WHERE owner_id=?', (guest,))


class SeenInput(BaseModel):
    species: list[FlowerSpecies] = Field(min_length=1, max_length=6)


def install(app):
    @app.get('/api/v1/flowers')
    def get_flowers():
        with auth.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            return catalog(db, owner_id())

    @app.post('/api/v1/flowers/seen')
    def mark_seen(data: SeenInput):
        with auth.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            updated = 0
            for species in set(data.species):
                updated += db.execute('''UPDATE flower_unlocks SET seen_at=?
                    WHERE owner_id=? AND species=? AND seen_at IS NULL''',
                                      (timestamp(), owner_id(), species)).rowcount
            return {'updated': updated}
