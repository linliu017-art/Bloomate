"""Apply full CloudRun configuration without credentials in OS process arguments."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time


def api(action, body):
    cli = str(Path(shutil.which('tcb')).resolve())
    with tempfile.TemporaryDirectory(prefix='bloomate-api-') as tmp:
        wrapper = Path(tmp) / 'invoke.cjs'
        request = Path(tmp) / 'request.json'
        wrapper.write_text("const fs=require('node:fs'); const r=JSON.parse(fs.readFileSync(process.argv[2],'utf8')); process.argv=['node',r.cli,'api','tcbr',r.action,'--api-version','2022-02-17','--body',JSON.stringify(r.body),'--json'];require(r.cli);")
        request.write_text(json.dumps({'cli': cli, 'action': action, 'body': body}))
        request.chmod(0o600)
        result = subprocess.run(['node', str(wrapper), str(request)], capture_output=True, text=True, timeout=60)
    text = result.stdout
    try:
        obj = json.loads(text[text.index('{'):])
        if result.returncode or 'data' not in obj:
            raise ValueError()
        return obj['data']
    except (ValueError, KeyError):
        raise RuntimeError(action + ' failed; raw response suppressed') from None


def read_env(path):
    result = {}
    for line in Path(path).read_text().splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            k, v = line.split('=', 1)
            result[k] = v
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--service', choices=['bloomate-api', 'bloomate-web'], required=True)
    parser.add_argument('--config', required=True)
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--inspect', action='store_true')
    args = parser.parse_args()
    env_id = 'prd-template-demo-d0dnb1e3069662'
    base = {'EnvId': env_id, 'ServerName': args.service}
    if args.inspect:
        detail = api('DescribeCloudRunServerDetail', base)
        detail = {**detail.get('BaseInfo', {}), **detail.get('ServerConfig', {})}
        # Never print EnvParams or platform-injected credentials.
        print(json.dumps({k: v for k, v in detail.items() if k in {
            'DefaultDomainName', 'ServerName', 'Status', 'Cpu', 'Mem', 'MinNum', 'MaxNum', 'CpuSpecs', 'MemSpecs', 'Port'}}, ensure_ascii=False))
        return
    values = read_env(args.config)
    assert values and all('\n' not in v for v in values.values())
    records = api('DescribeCloudRunDeployRecord', base)['DeployRecords']
    image = max(records, key=lambda r: r['DeployTime'])['ImageUrl']
    items = [{'Key': 'EnvParam', 'Value': json.dumps(values)},
             {'Key': 'CpuSpecs', 'FloatValue': 0.25}, {'Key': 'MemSpecs', 'FloatValue': 0.5},
             {'Key': 'MinNum', 'IntValue': 0}, {'Key': 'MaxNum', 'IntValue': 1},
             {'Key': 'Port', 'IntValue': args.port}]
    task_id = api('UpdateCloudRunServer', {**base, 'DeployInfo': {
        'DeployType': 'image', 'ImageUrl': image, 'ReleaseType': 'FULL'}, 'Items': items})['TaskId']
    print(json.dumps({'service': args.service, 'task': task_id, 'variable_names': sorted(values),
                      'cpu': 0.25, 'memory': 0.5, 'instances': '0..1'}), flush=True)
    for _ in range(60):
        time.sleep(10)
        status = api('DescribeServerManageTask', {**base, 'TaskId': task_id})
        status = (status.get('Task') or status).get('Status')
        if status in ('finished', 'failed'):
            print(json.dumps({'service': args.service, 'status': status}), flush=True)
            raise SystemExit(0 if status == 'finished' else 1)
    raise SystemExit('Deployment task pending; inspect by task id.')


if __name__ == '__main__':
    main()
