-- Cloud PostgreSQL is authoritative. No browser has table or RPC access.
BEGIN;
CREATE TABLE IF NOT EXISTS public.bloomate_state (
 id integer PRIMARY KEY CHECK (id=1), revision bigint NOT NULL DEFAULT 0,
 checkpoint jsonb, updated_at timestamptz NOT NULL DEFAULT now());
INSERT INTO public.bloomate_state(id) VALUES (1) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS public.bloomate_commits (
 operation_id text PRIMARY KEY, revision bigint NOT NULL, checksum text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS public.bloomate_erasures (
 owner_hash text PRIMARY KEY CHECK(owner_hash ~ '^[a-f0-9]{64}$'),
 operation_id text NOT NULL CHECK(operation_id ~ '^[a-f0-9]{48}$'), created_at bigint NOT NULL);
ALTER TABLE public.bloomate_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.bloomate_commits ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.bloomate_erasures ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.bloomate_state,public.bloomate_commits,public.bloomate_erasures FROM PUBLIC,anon,authenticated;
GRANT SELECT ON public.bloomate_state TO service_role;
GRANT SELECT,INSERT,UPDATE ON public.bloomate_state TO service_role;
GRANT SELECT,INSERT,DELETE ON public.bloomate_commits TO service_role;
GRANT SELECT,INSERT ON public.bloomate_erasures TO service_role;

CREATE OR REPLACE FUNCTION public.bloomate_commit_state(p_expected bigint,p_checkpoint jsonb,p_operation text)
RETURNS bigint LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
DECLARE current_revision bigint; saved_revision bigint; saved_checksum text;
BEGIN
 IF p_operation !~ '^[a-f0-9]{32}$' OR p_checkpoint->>'format' IS DISTINCT FROM 'sqlite-zlib-v1'
  OR p_checkpoint->>'sha256' !~ '^[a-f0-9]{64}$'
  OR length(p_checkpoint->>'data')>4194304 OR (p_checkpoint->>'raw_bytes')::bigint>33554432 THEN
  RAISE EXCEPTION 'Invalid checkpoint' USING ERRCODE='22023';
 END IF;
 SELECT revision INTO current_revision FROM public.bloomate_state WHERE id=1 FOR UPDATE;
 SELECT revision,checksum INTO saved_revision,saved_checksum FROM public.bloomate_commits WHERE operation_id=p_operation;
 IF FOUND THEN
  IF saved_checksum <> p_checkpoint->>'sha256' THEN RAISE EXCEPTION 'Operation mismatch' USING ERRCODE='22023'; END IF;
  RETURN saved_revision;
 END IF;
 IF current_revision<>p_expected THEN RAISE EXCEPTION 'Revision conflict' USING ERRCODE='PT409'; END IF;
 UPDATE public.bloomate_state SET revision=revision+1,checkpoint=p_checkpoint,updated_at=now() WHERE id=1;
 INSERT INTO public.bloomate_commits VALUES(p_operation,current_revision+1,p_checkpoint->>'sha256',now());
 DELETE FROM public.bloomate_commits WHERE created_at<now()-interval '7 days';
 RETURN current_revision+1;
END $$;

CREATE OR REPLACE FUNCTION public.bloomate_append_erasures(p_hashes text[],p_operation text)
RETURNS void LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
BEGIN
 IF p_operation !~ '^[a-f0-9]{48}$' OR cardinality(p_hashes)>1000 OR cardinality(p_hashes)<1 THEN
  RAISE EXCEPTION 'Invalid erasure' USING ERRCODE='22023';
 END IF;
 INSERT INTO public.bloomate_erasures(owner_hash,operation_id,created_at)
 SELECT h,p_operation,extract(epoch from now())::bigint FROM unnest(p_hashes) h ON CONFLICT DO NOTHING;
END $$;
CREATE OR REPLACE FUNCTION public.bloomate_read_erasures()
RETURNS jsonb LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
 SELECT coalesce(jsonb_agg(to_jsonb(e)),'[]'::jsonb) FROM public.bloomate_erasures e;
$$;
REVOKE ALL ON FUNCTION public.bloomate_commit_state(bigint,jsonb,text), public.bloomate_append_erasures(text[],text),public.bloomate_read_erasures() FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.bloomate_commit_state(bigint,jsonb,text), public.bloomate_append_erasures(text[],text),public.bloomate_read_erasures() TO service_role;
NOTIFY pgrst,'reload schema';
COMMIT;
