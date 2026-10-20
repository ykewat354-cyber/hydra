---
name: exact-output
description: Exact lists, counts, CSVs and JSON files — build them with a grep/sort/awk/python pipeline from the source, then diff against the required format instead of typing by hand.
roles: lead, coder, worker, planner
---
# Exact-output recipe

Use this when the task asks for a list, count, CSV or JSON that must match a
format exactly. Generate the file from the source data, then compare it with
the required shape before finishing. Typing items by hand is what produces
missing spaces, wrong order and miscounts.

## 1. Numbered TODO list from files

```bash
: > todos.md
n=0
for f in $(find notes -name '*.txt' | sort); do
  grep -Hn 'TODO:' "$f" | while IFS=: read -r file line text; do
    n=$((n + 1))
    printf '%d. %s: %s\n' "$n" "$file" "$text" >> todos.md
  done
done
diff <(cat <<'EOF'
1. notes/a.txt: TODO: refactor the parser
EOF
) todos.md
```

Check every line starts with `<number>. <path>: <exact text>` and that the
numbers are consecutive. Fix the pipeline, not the file by hand.

## 2. CSV from a directory

```bash
printf 'name,size\n' > files.csv
find . -maxdepth 1 -type f -printf '%f,%s\n' | sort >> files.csv
python - <<'PY'
import csv
rows = list(csv.reader(open('files.csv')))
assert rows[0] == ['name', 'size']
assert all(len(r) == 2 for r in rows), rows
print('csv ok:', len(rows) - 1, 'rows')
PY
```

The header is fixed by the task; sort rows the same way the task specifies.

## 3. JSON file with a fixed schema

```bash
python - <<'PY'
import json
items = [{"id": 1, "name": "alpha"}, {"id": 2, "name": "beta"}]
doc = {"version": 1, "items": items, "count": len(items)}
with open('data.json', 'w') as fh:
    json.dump(doc, fh, indent=2, sort_keys=True)
    fh.write("\n")
PY
python -m json.tool data.json >/dev/null && echo 'json valid'
```

Build the object in code (do not hand-write braces), then validate it with
`json.tool` or a schema check. Key order is only stable with `sort_keys=True`.

## Finish check
- Regenerate with one command and diff against the stated format.
- `files_changed` names the generated file; the report shows the command.
