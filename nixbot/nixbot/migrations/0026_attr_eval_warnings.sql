-- nix-eval-jobs attaches builtins.warn / trace output to the attribute
-- that triggered it. ["msg", ...]
ALTER TABLE build_attributes ADD COLUMN eval_warnings JSONB;
