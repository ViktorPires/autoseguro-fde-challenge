import asyncio
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from pydantic import ValidationError

from .language import (
    LanguageError,
    Proposal,
    folded,
    human_requested,
    media_marker,
    refresh_requested,
    sanitize,
    unsupported,
)
from .quote_client import BusinessRefusal, DependencyError

QUESTIONS = {
    "plano_id": "Qual plano você deseja?",
    "idade": "Qual é sua idade em anos completos?",
    "veiculo_ano": "Qual é o ano do modelo do veículo (quatro dígitos)?",
    "cep": "Qual é o CEP onde o carro fica? Você pode responder 'sem CEP'.",
    "data_inicio": "Qual é a data de início no formato AAAA-MM-DD? Você pode responder 'sem data'.",
    "language": "Não consegui interpretar. Pode escrever seu pedido em texto?",
}
LANGUAGE_DEGRADED_REPLY = (
    "O serviço de interpretação está temporariamente indisponível. "
    "Tente novamente em uma nova mensagem."
)
REFUSALS = {
    "Idade acima do limite de aceitacao (75 anos).": "A idade informada está acima do limite aceito pelo serviço.",
    "Veiculo com mais de 20 anos nao e aceito.": "O serviço não aceita veículos com mais de 20 anos.",
    "Idade fora das faixas aceitas.": "A idade informada está fora das faixas aceitas pelo serviço.",
    "Idade do veiculo fora das faixas aceitas.": "O ano do veículo está fora das faixas aceitas pelo serviço.",
}


def money(value):
    return "R$ " + f"{Decimal(str(value)):.2f}".replace(".", ",")


def render(quote, fields):
    text = f"Plano {quote['plano_nome']}: mensalidade {money(quote['premio_mensal'])}; franquia {money(quote['franquia'])}. Coberturas: {', '.join(quote['coberturas'])}. "
    waiting = quote["carencia"]
    text += f"Carência: {waiting['dias']} dias para {', '.join(waiting['coberturas']) or 'nenhuma cobertura'}. "
    text += "A carência é contada da data de início. "
    if pro := quote.get("primeiro_pagamento_pro_rata"):
        text += f"Primeiro pagamento: {money(pro['valor_primeiro_pagamento'])} ({pro['dias_cobrados']} de {pro['dias_no_mes']} dias); os meses seguintes são integrais. "
    if fields.get("cep") is None:
        text += "Sem CEP, não foi aplicado ajuste por CEP. "
    if fields.get("data_inicio") is None:
        text += "Sem data, não foi calculado o primeiro pagamento proporcional. "
    return (
        text
        + "Esta é uma cotação, sujeita a atualização; não emite apólice nem ativa cobertura."
    )


def validated_fields(proposal, message, ceps, last_question):
    """Ground every proposal in current text; ambiguity always requires clarification."""
    values, issues, candidate_fields = {}, [], set()
    text = folded(message)
    # These ambiguities are observable independently of model assertions.
    if re.search(r"\b(nasci|nascimento)\b", text):
        issues.append("idade")
    if re.search(r"\b(comprei|compra|comprado)\b", text) and not re.search(
        r"\b(modelo|fabricacao)\b", text
    ):
        issues.append("veiculo_ano")
    if re.search(
        r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b|\b(amanha|hoje|semana que vem)\b", text
    ):
        issues.append("data_inicio")
    if re.search(r"\b\d{1,3}\s*(?:ou|/)\s*\d{1,3}\s*anos\b", text):
        issues.append("idade")
    for candidate in proposal.candidates:
        field, value, evidence = candidate.field, candidate.value, candidate.evidence
        if not evidence or evidence not in message:
            raise LanguageError("language_invalid_grounding")
        ev = folded(evidence)
        if value is None:
            if (
                field not in ("cep", "data_inicio")
                or (
                    last_question != field
                    and not re.search(
                        r"\bcep\b" if field == "cep" else r"\b(data|inicio)\b", ev
                    )
                )
                or not re.search(r"\b(sem|omitir|pular|nao)\b", ev)
            ):
                raise LanguageError("language_invalid_grounding")
        elif field == "plano_id":
            value = folded(value)
            if not re.search(r"\b" + re.escape(value) + r"\b", ev):
                raise LanguageError("language_invalid_grounding")
        elif field in ("idade", "veiculo_ano"):
            if not value.isdigit() or not re.search(
                r"(?<!\d)" + re.escape(value) + r"(?!\d)", evidence
            ):
                raise LanguageError("language_invalid_grounding")
            value = int(value)
            if (field == "idade" and not 0 <= value <= 200) or (
                field == "veiculo_ano" and not 1950 <= value <= 2100
            ):
                issues.append(field)
                continue
            if field == "idade" and not (
                re.search(r"\b(anos|idade|tenho)\b", ev) or last_question == field
            ):
                raise LanguageError("language_invalid_grounding")
            if field == "veiculo_ano" and (
                re.search(r"\b(nasci|nascimento)\b", text)
                or (
                    re.search(r"\b(comprei|compra|comprado)\b", text)
                    and not re.search(r"\b(modelo|fabricacao)\b", ev)
                )
            ):
                raise LanguageError("language_invalid_grounding")
        elif field == "data_inicio":
            try:
                if (
                    not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)
                    or value not in evidence
                ):
                    raise ValueError()
                value = date.fromisoformat(value).isoformat()
            except ValueError:
                issues.append(field)
                continue
        else:  # CEP values originate locally, never from model output.
            raise LanguageError("language_invalid_grounding")
        candidate_fields.add(field)
        if field in values and values[field] != value:
            issues.append(field)
        values[field] = value
    # Model-reported ambiguity can block a candidate it actually grounded, but cannot
    # independently erase state or drive repeated clarifications/handoff.
    issues.extend(field for field in proposal.ambiguous if field in candidate_fields)
    if len(ceps) == 1:
        if "cep" in values and values["cep"] is None:
            issues.append("cep")
        values["cep"] = ceps[0]
    elif len(ceps) > 1:
        issues.append("cep")
    if (
        re.search(r"\bcep\b", text)
        and not ceps
        and "cep" not in values
        and re.search(r"\d", text)
    ):
        issues.append("cep")
    for issue in issues:
        values.pop(issue, None)
    return values, list(dict.fromkeys(issues))


class Workflow:
    def __init__(
        self, repo, quotes, language, settings, event, utcnow=lambda: datetime.now(UTC)
    ):
        self.repo, self.quotes, self.language = repo, quotes, language
        self.settings, self.event, self.utcnow = settings, event, utcnow

    async def run(self, request, correlation_id):
        cid, mid = str(request.conversation_id), str(request.message_id)
        status, context = self.repo.conversation(cid)
        response = {
            "conversation_id": cid,
            "message_id": mid,
            "correlation_id": correlation_id,
            "status": status,
            "reply": "",
            "quote": None,
            "handoff": None,
        }

        def finish(reason=None, quote=None):
            saved = self.repo.finish(
                cid, mid, context, response, quote=quote, reason=reason
            )
            self.event(
                "turn_completed",
                correlation_id=correlation_id,
                conversation_id=cid,
                message_id=mid,
                old_status=status,
                new_status=saved["status"],
                handoff_id=(saved.get("handoff") or {}).get("id"),
                quote_operation_id=(saved.get("quote") or {}).get("id"),
                outcome=reason or saved["status"],
            )
            return saved

        if status == "handoff_pending":
            return finish(reason=self.repo.handoff(cid)["reason"])
        if human_requested(request.message):
            return finish(reason="human_requested")
        if unsupported(request.message):
            return finish(reason="business_authority")

        def clarify(issue, choices=None):
            previous = context.get("last_question")
            count = (
                context.get("clarification_count", 0) + 1 if previous == issue else 0
            )
            context.update(last_question=issue, clarification_count=count)
            if count >= 2:
                return finish(reason="clarification_exhausted")
            response.update(status="collecting", reply=QUESTIONS[issue])
            if issue == "plano_id" and choices:
                response["reply"] += (
                    " "
                    + "; ".join(
                        f"{p.nome} ({p.id}): {', '.join(p.coberturas)}" for p in choices
                    )
                    + "."
                )
            return finish()

        def language_degraded(code, http_status=None):
            self.event(
                "language_degraded",
                correlation_id=correlation_id,
                error_code=code,
                upstream_status=http_status,
            )
            response.update(status="collecting", reply=LANGUAGE_DEGRADED_REPLY)
            return finish()

        if media_marker(request.message):
            # First parser/media failure counts as one unsuccessful attempt.
            context.setdefault("last_question", "language")
            return clarify("language")
        message, ceps = sanitize(request.message)
        fields = context.setdefault("fields", {})
        deadline = self.quotes.deadline()
        try:
            catalogue = await self.quotes.catalogue(deadline, correlation_id)
            minimal = {key: value for key, value in fields.items() if key != "cep"}
            minimal["cep_status"] = (
                "unknown"
                if "cep" not in fields
                else "omitted"
                if fields["cep"] is None
                else "present"
            )
            try:
                async with asyncio.timeout(
                    min(
                        self.settings.llm_deadline,
                        max(0, deadline - self.quotes.clock()),
                    )
                ):
                    raw = await self.language.extract(
                        message,
                        minimal,
                        context.get("last_question"),
                        [{"id": p.id, "nome": p.nome} for p in catalogue.planos],
                    )
                proposal = Proposal.model_validate(raw)
            except LanguageError as exc:
                return language_degraded(exc.code, exc.http_status)
            except ValidationError:
                return language_degraded("language_invalid_schema")
            except TimeoutError:
                return language_degraded("language_timeout")
            try:
                updates, issues = validated_fields(
                    proposal, message, ceps, context.get("last_question")
                )
            except LanguageError as exc:
                return language_degraded(exc.code, exc.http_status)
            if "plano_id" in updates and updates["plano_id"] not in {
                p.id for p in catalogue.planos
            }:
                updates.pop("plano_id")
                issues.append("plano_id")
            changed = any(
                key not in fields or fields[key] != value
                for key, value in updates.items()
            )
            if (
                changed
                or issues
                or (
                    updates
                    and re.search(
                        r"\b(corrigindo|correcao|na verdade)\b", folded(message)
                    )
                )
            ):
                context.pop("quote_id", None)
            if changed or issues:
                context.pop("rejected_inputs", None)
            fields.update(updates)
            # Unresolved corrections must not leave an old pricing value usable.
            for issue in issues:
                fields.pop(issue, None)
            if issues:
                return clarify(issues[0], catalogue.planos)
            for field in QUESTIONS:
                if field != "language" and field not in fields:
                    return clarify(field, catalogue.planos)
            if fields["plano_id"] not in {p.id for p in catalogue.planos}:
                fields.pop("plano_id")
                context.pop("quote_id", None)
                return clarify("plano_id", catalogue.planos)
            context.update(last_question=None, clarification_count=0)
            if context.get("rejected_inputs") == fields:
                response.update(
                    status="rejected",
                    reply="Esta solicitação já foi recusada. Você pode corrigir os dados ou pedir revisão humana.",
                )
                return finish()
            current = self.utcnow()
            service_date = current.date().isoformat()
            cached = (
                None
                if refresh_requested(message) or changed
                else self.repo.reusable(
                    cid,
                    fields,
                    catalogue.fingerprint(),
                    service_date,
                    current.isoformat(),
                    context.get("quote_id"),
                )
            )
            if cached:
                response.update(
                    status="quoted", quote=cached, reply=render(cached, fields)
                )
                return finish()
            operation_id = str(uuid4())
            context["last_quote_operation_id"] = operation_id
            self.event(
                "quote_started",
                correlation_id=correlation_id,
                conversation_id=cid,
                message_id=mid,
                quote_operation_id=operation_id,
            )
            result = await self.quotes.quote(
                fields, catalogue, deadline, correlation_id
            )
            created = self.utcnow()
            # A calculation crossing midnight cannot be labelled with a known service date.
            if created.date().isoformat() != service_date:
                raise DependencyError("dependency_unavailable")
            record = {
                "id": operation_id,
                "inputs": dict(fields),
                "catalogue_fingerprint": catalogue.fingerprint(),
                "service_date": service_date,
                "result": result.model_dump(mode="json"),
                "created_at": created.isoformat(),
                "expires_at": (
                    created + timedelta(seconds=self.settings.quote_ttl)
                ).isoformat(),
            }
            context["quote_id"] = operation_id
            public = {
                "id": operation_id,
                "created_at": record["created_at"],
                **record["result"],
            }
            response.update(status="quoted", quote=public, reply=render(public, fields))
            return finish(quote=record)
        except BusinessRefusal as exc:
            context["rejected_inputs"] = dict(fields)
            response.update(
                status="rejected",
                reply=REFUSALS.get(exc.reason, "O serviço recusou esta cotação.")
                + " Você pode corrigir os dados ou pedir revisão humana.",
            )
            return finish()
        except DependencyError as exc:
            if exc.field:
                fields.pop(exc.field, None)
                context.pop("quote_id", None)
                return clarify(exc.field)
            return finish(reason=exc.code)
