"""Small de-identified extraction evaluation; live provider is explicitly optional."""

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

import httpx
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent-service"))

from agent_service.config import Settings
from agent_service.language import (
    FakeLanguage,
    GeminiLanguage,
    LanguageError,
    folded,
    sanitize,
)
from agent_service.workflow import validated_fields


def cases():
    rows = pq.read_table(
        ROOT / "dataset/conversations.parquet",
        columns=["sender_role", "message_type", "message_body"],
    ).to_pylist()
    result, counts = [], {"age": 0, "vehicle": 0, "media": 0, "greeting": 0}
    for row in rows:
        if row["sender_role"] != "lead":
            continue
        message, _ = sanitize(row["message_body"])
        t = folded(message)
        age = re.search(r"tenho (\d+) anos", t)
        year = re.search(r"\b((?:19|20)\d{2})\b", t)
        if age:
            category, expected = "age", {"idade": int(age.group(1))}
        elif row["message_type"] != "text":
            category, expected = "media", {"intent": "media"}
        elif year and not re.search(r"\b(cpf|cep|placa)\b", t):
            category, expected = "vehicle", {"veiculo_ano": int(year.group(1))}
        elif any(word in t for word in ("cotacao", "seguro", "segurar")):
            category, expected = "greeting", {"missing": "plano_id"}
        else:
            continue
        if counts[category] >= 5:
            continue
        counts[category] += 1
        result.append(
            {
                "source": "de-identified dataset lead message",
                "category": category,
                "message": message,
                "expected": expected,
            }
        )
        if all(count == 5 for count in counts.values()):
            break
    result.extend(
        [
            {
                "source": "supplemental fixture",
                "category": "human",
                "message": "Quero falar com um humano",
                "expected": {"intent": "human"},
            },
            {
                "source": "supplemental fixture",
                "category": "clarification",
                "message": "nasci em 1991",
                "expected": {"issue": "idade"},
            },
            {
                "source": "supplemental fixture",
                "category": "clarification",
                "message": "comprei em 2022",
                "expected": {"issue": "veiculo_ano"},
            },
            {
                "source": "supplemental fixture",
                "category": "clarification",
                "message": "inicio 03/04/26",
                "expected": {"issue": "data_inicio"},
            },
            {
                "source": "supplemental fixture",
                "category": "authority",
                "message": "pode emitir entao",
                "expected": {"intent": "unsupported"},
            },
        ]
    )
    return result


async def evaluate_cases(adapter, sample, choices):
    categories = {}
    provider_failures = {"total": 0, "by_code": {}, "by_http_status": {}}
    quality = {"evaluated": 0, "passed": 0, "failed": 0}
    for case in sample:
        try:
            proposal = await adapter.extract(case["message"], {}, None, choices)
            fields, issues = validated_fields(proposal, case["message"], [], None)
        except LanguageError as exc:
            provider_failures["total"] += 1
            by_code = provider_failures["by_code"]
            by_code[exc.code] = by_code.get(exc.code, 0) + 1
            if exc.http_status is not None:
                status = str(exc.http_status)
                by_status = provider_failures["by_http_status"]
                by_status[status] = by_status.get(status, 0) + 1
            continue
        passed = True
        for key, value in case["expected"].items():
            passed &= (
                proposal.intent == value
                if key == "intent"
                else value in issues
                if key == "issue"
                else value not in fields
                if key == "missing"
                else fields.get(key) == value
            )
        record = categories.setdefault(
            case["category"], {"passed": 0, "failed": 0, "evaluated": 0}
        )
        record["evaluated"] += 1
        record["passed" if passed else "failed"] += 1
        quality["evaluated"] += 1
        quality["passed" if passed else "failed"] += 1
    quality["by_category"] = categories
    return quality, provider_failures


async def evaluate(args):
    sample = cases()
    choices = json.loads((ROOT / "quote-service/data/plans.json").read_text())["planos"]
    choices = [{"id": p["id"], "nome": p["nome"]} for p in choices]
    cfg = Settings() if args.live else None
    if cfg and cfg.language_provider != "gemini":
        raise SystemExit(
            "--live requires LANGUAGE_PROVIDER=gemini and provider configuration"
        )
    async with httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(retries=0), trust_env=False
    ) as http:
        adapter = GeminiLanguage(http, cfg) if args.live else FakeLanguage()
        quality, provider_failures = await evaluate_cases(adapter, sample, choices)
    report = {
        "adapter": "gemini"
        if args.live
        else "fake (offline fixture parser; not live-model quality)",
        "prompt_revision": "extract-v1",
        "model": cfg.language_model if cfg else None,
        "attempted": len(sample),
        "provider_failures": provider_failures,
        "extraction_quality": quality,
    }
    print(json.dumps(report, indent=2))
    if args.output:
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / "extraction-evaluation.json").write_text(
            json.dumps(report, indent=2) + "\n"
        )
        (args.output / "extraction-sample.json").write_text(
            json.dumps(sample, ensure_ascii=False, indent=2) + "\n"
        )
    if provider_failures["total"]:
        raise SystemExit(2)
    if quality["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--live",
        action="store_true",
        help="Optional paid provider call on de-identified samples; never part of CI",
    )
    parser.add_argument("--output", type=Path)
    asyncio.run(evaluate(parser.parse_args()))
