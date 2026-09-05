#!/bin/sh
# 验证 mac-mini 的只读检索端点：关键词命中要有片段，闲聊要 is_ncc=false 且 facts 仍在。
set -e
for q in "大理据点现在还能去吗" "写一封辞职信"; do
  echo "== $q"
  curl -s -X POST http://100.71.182.5:8434/retrieve -H 'Content-Type: application/json' \
    -d "{\"query\":\"$q\"}" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("is_ncc=",d["is_ncc"],"trigger=",d["meta"].get("trigger"),"context_len=",len(d["context"]),"facts_len=",len(d["facts"]))'
done
