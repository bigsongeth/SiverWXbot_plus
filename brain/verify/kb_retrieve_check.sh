#!/bin/sh
# 验证 mac-mini 的只读检索端点：关键词命中要 trigger=关键词 且有片段；第二条是通用问题，
# 闸门刻意偏宽（阈值 0.32，见 CLAUDE.md 3.7），trigger 为「相似度」或「未命中」都正常，重点看 facts_len>0。
# 注意 query 是手拼 JSON，别放含引号/反斜杠的问题。
set -e
for q in "大理据点现在还能去吗" "写一封辞职信"; do
  echo "== $q"
  curl -s -X POST http://100.71.182.5:8434/retrieve -H 'Content-Type: application/json' \
    -d "{\"query\":\"$q\"}" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("is_ncc=",d["is_ncc"],"trigger=",d["meta"].get("trigger"),"context_len=",len(d["context"]),"facts_len=",len(d["facts"]))'
done
