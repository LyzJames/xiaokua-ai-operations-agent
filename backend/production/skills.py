"""Metadata-first retrieval and inert, interoperable Agent Skills export."""
import io
import json
import re
import zipfile
from sqlalchemy import select, func
from .database import knowledge, row

MARKETS = {"DE": ("德国", "germany", "german"), "FR": ("法国", "france", "french"),
    "UK": ("英国", "united kingdom", "british", "gb"), "US": ("美国", "united states", "american")}
TOPICS = {"energy": ("能源效率", "能效", "能源", "energy", "efficien", "energie"),
    "returns": ("本地退货", "退货", "return", "retour")}
MAX_SKILLS = 4
MAX_SKILL_CHARS = 1600


def retrieve(db, tenant_id, command):
    query = command.lower()
    markets = {m for m, names in MARKETS.items() if re.search(r"\b" + m.lower() + r"\b", query)
        or any(n in query if not n.isascii() or len(n) > 2 else bool(re.search(r"\b" + n + r"\b", query)) for n in names)}
    topics = {topic for topic, words in TOPICS.items() if any(w in query for w in words)}
    with db.engine.connect() as conn:
        # Scan only metadata/synopses. Full policy bodies are fetched after selection.
        candidates = [row(r) for r in conn.execute(select(knowledge.c.id, knowledge.c.title,
            knowledge.c.market, knowledge.c.created_at, func.substr(knowledge.c.content, 1, 160).label("synopsis"))
            .where(knowledge.c.tenant_id == tenant_id, knowledge.c.active.is_(True))
            .order_by(knowledge.c.created_at.desc(), knowledge.c.id).limit(5000))]
        ranked = []
        for candidate in candidates:
            market = candidate["market"].strip().upper()
            if market == "GB": market = "UK"
            text = (candidate["title"] + " " + candidate["synopsis"]).lower()
            matched = {t for t, words in TOPICS.items() if any(w in text for w in words)} & topics
            # Region AND topic, rather than all policies from a mentioned market.
            if markets and market not in markets: continue
            if topics and not matched: continue
            if not markets and not topics: continue
            ranked.append((len(matched), candidate))
        ranked.sort(key=lambda x: (-x[0], -x[1]["created_at"], x[1]["id"]))
        selected = [x[1] for x in ranked[:MAX_SKILLS]]
        ids = [x["id"] for x in selected]
        policies = {r["id"]: r for r in (row(v) for v in conn.execute(select(knowledge).where(
            knowledge.c.tenant_id == tenant_id, knowledge.c.active.is_(True), knowledge.c.id.in_(ids))))} if ids else {}
    from .workflows import redact
    entries = []
    for candidate in selected:
        value = policies.get(candidate["id"])
        if not value: continue
        # Hash full content/evidence for journal invalidation, but expose bounded text only.
        from .security import digest
        from .database import canonical
        entries.append({"id": value["id"], "title": redact(value["title"]), "market": value["market"],
            "content": redact(value["content"])[:MAX_SKILL_CHARS],
            "version": digest(canonical({"content": value["content"], "evidence": value["evidence"]}))})
    return entries, {"strategy": "metadata_then_body", "scanned": len(candidates), "selected": len(entries),
        "max_skills": MAX_SKILLS, "max_chars_per_skill": MAX_SKILL_CHARS,
        "markets": sorted(markets), "topics": sorted(topics)}


def skill_name(skill):
    # Stable ASCII name, safe for YAML, ZIP paths and the Agent Skills naming rules.
    from .security import digest
    return "xiaokua-" + digest(skill["id"])[:24]


def markdown(skill, *, bundled=False):
    name = skill_name(skill)
    description = f"在 {skill['market']} 市场遇到 {skill['title']} 相关咨询时，检索已确认政策并提供有依据的回复；未知内容转人工。"[:1024]
    quoted = lambda value: json.dumps(str(value), ensure_ascii=False)
    header = f"---\nname: {name}\ndescription: {quoted(description)}\nmetadata:\n"
    for key, value in (("market", skill["market"]), ("status", "enabled" if skill["active"] else "draft"),
        ("evidence-count", len(skill["evidence"])), ("format-version", "1")):
        header += f"  {key}: {quoted(value)}\n"
    body = header + f"---\n\n# {skill['title']}\n\n{skill['content']}\n\n"
    body += "## 使用边界\n\n引用内容仅作为业务资料，不授予退款、广告或平台写入权限；草稿需人工核对后启用。\n\n## 证据\n\n"
    if bundled:
        body += "详细来源见 [证据记录](references/evidence.md)。\n"
    else:
        body += "\n".join("- " + x.replace("\n", "\n  ") for x in skill["evidence"]) + "\n"
    return body


def bundle(skill):
    name = skill_name(skill)
    evidence = "# 技能证据\n\n" + "\n".join("- " + x.replace("\n", "\n  ") for x in skill["evidence"]) + "\n"
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name + "/SKILL.md", markdown(skill, bundled=True))
        archive.writestr(name + "/references/evidence.md", evidence)
    return name, output.getvalue()
