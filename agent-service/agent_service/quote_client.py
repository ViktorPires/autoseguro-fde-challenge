import asyncio
import hashlib
import random
import ssl
import time
from calendar import monthrange
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

import httpx
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    ValidationError,
    model_validator,
)

from .repository import canonical


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError("numeric value required")
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError("finite nonnegative number required")
    return result


Money = Annotated[Decimal, BeforeValidator(number)]
Texts = Annotated[list[StrictStr], Field(min_length=1, max_length=100)]
NonnegativeInt = Annotated[StrictInt, Field(ge=0, le=10000)]


class ServiceModel(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)


class Plan(ServiceModel):
    id: StrictStr
    nome: StrictStr
    base_mensal: Money
    franquia: Money
    coberturas: Texts


class AgeBand(ServiceModel):
    idade_min: NonnegativeInt
    idade_max: NonnegativeInt
    multiplicador: Money | None = None
    recusar: bool = Field(default=False, strict=True)
    motivo: StrictStr | None = None

    @model_validator(mode="after")
    def complete(self):
        if (
            self.idade_min > self.idade_max
            or (self.recusar and not self.motivo)
            or (not self.recusar and self.multiplicador is None)
        ):
            raise ValueError("invalid band")
        return self


class VehicleBand(ServiceModel):
    anos_min: NonnegativeInt
    anos_max: NonnegativeInt
    multiplicador: Money | None = None
    recusar: bool = Field(default=False, strict=True)
    motivo: StrictStr | None = None

    @model_validator(mode="after")
    def complete(self):
        if (
            self.anos_min > self.anos_max
            or (self.recusar and not self.motivo)
            or (not self.recusar and self.multiplicador is None)
        ):
            raise ValueError("invalid band")
        return self


class Region(ServiceModel):
    prefixos_alto_risco: list[Annotated[StrictStr, Field(pattern=r"^\d{2}$")]]
    multiplicador: Money


class WaitingRule(ServiceModel):
    coberturas_com_carencia: Texts
    dias: NonnegativeInt
    observacao: StrictStr = Field(alias="_obs")


class EntryRule(ServiceModel):
    regra: Literal["pro_rata_primeiro_mes"]


class Rules(ServiceModel):
    faixa_etaria: Annotated[list[AgeBand], Field(min_length=1)]
    idade_veiculo: Annotated[list[VehicleBand], Field(min_length=1)]
    regiao_cep: Region
    carencia: WaitingRule
    entrada_meio_mes: EntryRule


class Catalogue(ServiceModel):
    moeda: Literal["BRL"]
    planos: Annotated[list[Plan], Field(min_length=1)]
    regras: Rules

    @model_validator(mode="after")
    def unique(self):
        if len({p.id for p in self.planos}) != len(self.planos):
            raise ValueError("duplicate plan")
        return self

    def fingerprint(self):
        return hashlib.sha256(
            canonical(self.model_dump(mode="json")).encode()
        ).hexdigest()


class Multipliers(ServiceModel):
    faixa_etaria: Money
    idade_veiculo: Money
    regiao: Money


class Waiting(ServiceModel):
    coberturas: list[StrictStr]
    dias: NonnegativeInt
    observacao: StrictStr


class Proration(ServiceModel):
    dias_no_mes: Annotated[StrictInt, Field(ge=28, le=31)]
    dias_cobrados: Annotated[StrictInt, Field(ge=1, le=30)]
    valor_primeiro_pagamento: Money


class QuoteResult(ServiceModel):
    plano_id: StrictStr
    plano_nome: StrictStr
    premio_mensal: Money
    franquia: Money
    coberturas: Texts
    multiplicadores: Multipliers
    carencia: Waiting
    moeda: Literal["BRL"]
    primeiro_pagamento_pro_rata: Proration | None = None

    def check_request(self, inputs, catalogue):
        plan = next((p for p in catalogue.planos if p.id == inputs["plano_id"]), None)
        if not plan or self.plano_id != plan.id or self.plano_nome != plan.nome:
            raise ValueError("unexpected plan")
        waiting = catalogue.regras.carencia
        if (
            self.coberturas != plan.coberturas
            or self.franquia != plan.franquia
            or self.carencia.dias != waiting.dias
            or self.carencia.coberturas
            != [c for c in plan.coberturas if c in waiting.coberturas_com_carencia]
        ):
            raise ValueError("inconsistent coverage")
        start = (
            date.fromisoformat(inputs["data_inicio"])
            if inputs.get("data_inicio")
            else None
        )
        pro = self.primeiro_pagamento_pro_rata
        if start and start.day != 1:
            days = monthrange(start.year, start.month)[1]
            if (
                not pro
                or pro.dias_no_mes != days
                or pro.dias_cobrados != days - start.day + 1
                or pro.valor_primeiro_pagamento > self.premio_mensal
            ):
                raise ValueError("invalid proration")
        elif pro is not None:
            raise ValueError("unexpected proration")
        return self


class Refusal(ServiceModel):
    error: Literal["cotacao_recusada"]
    motivo: StrictStr = Field(min_length=1, max_length=1000)


class DependencyError(Exception):
    def __init__(self, code="dependency_unavailable", field=None):
        self.code, self.field = code, field


class BusinessRefusal(Exception):
    def __init__(self, reason):
        self.reason = reason


CORRECTABLE_VALIDATION_TYPES = {
    "plano_id": {"missing", "string_type"},
    "idade": {
        "missing",
        "int_type",
        "int_parsing",
        "greater_than_equal",
        "less_than_equal",
    },
    "veiculo_ano": {
        "missing",
        "int_type",
        "int_parsing",
        "greater_than_equal",
        "less_than_equal",
    },
    "cep": {"string_type"},
    "data_inicio": {"string_type"},
}


class QuoteClient:
    def __init__(
        self,
        client,
        settings,
        event=lambda *a, **kw: None,
        clock=time.monotonic,
        sleep=asyncio.sleep,
        jitter=random.uniform,
    ):
        self.client, self.settings, self.event = client, settings, event
        self.clock, self.sleep, self.jitter = clock, sleep, jitter

    def deadline(self):
        return self.clock() + self.settings.quote_deadline

    async def operation(self, method, path, deadline, correlation_id, **kwargs):
        for attempt in range(1, self.settings.max_attempts + 1):
            remaining = deadline - self.clock()
            if remaining <= 0:
                break
            start = self.clock()
            status = None
            outcome = "dependency_unavailable"
            try:
                budget = min(self.settings.attempt_timeout, remaining)
                async with asyncio.timeout(budget):
                    response = await self.client.request(
                        method,
                        path,
                        headers={"X-Correlation-ID": correlation_id},
                        timeout=httpx.Timeout(
                            budget, connect=min(budget, self.settings.connect_timeout)
                        ),
                        **kwargs,
                    )
                status = response.status_code
                if status >= 500:
                    continue_retry = True
                else:
                    continue_retry = False
                    try:
                        payload = response.json()
                    except (ValueError, UnicodeError):
                        raise DependencyError("upstream_contract") from None
                    if (
                        status == 422
                        and method == "POST"
                        and path == "/quote"
                        and isinstance(payload, dict)
                        and payload.get("error") == "cotacao_recusada"
                    ):
                        try:
                            refusal = Refusal.model_validate(payload)
                        except ValidationError:
                            raise DependencyError("upstream_contract") from None
                        outcome = "business_refusal"
                        raise BusinessRefusal(refusal.motivo)
                    if status in (400, 422):
                        # Only an explicit, known input location is correctable.
                        details = (
                            payload.get("detail") if isinstance(payload, dict) else None
                        )
                        field = None
                        if (
                            isinstance(details, list)
                            and details
                            and all(
                                isinstance(d, dict)
                                and isinstance(d.get("loc"), list)
                                and all(
                                    isinstance(part, (str, int)) for part in d["loc"]
                                )
                                and isinstance(d.get("type"), str)
                                for d in details
                            )
                        ):
                            locations = {tuple(d["loc"]) for d in details}
                            if len(locations) == 1:
                                loc = next(iter(locations))
                                if (
                                    len(loc) == 2
                                    and loc[0] == "body"
                                    and loc[1]
                                    in {
                                        "idade",
                                        "veiculo_ano",
                                        "cep",
                                        "data_inicio",
                                        "plano_id",
                                    }
                                ):
                                    candidate = loc[1]
                                    if all(
                                        d["type"]
                                        in CORRECTABLE_VALIDATION_TYPES[candidate]
                                        for d in details
                                    ):
                                        field = candidate
                        raise DependencyError(
                            "upstream_input" if field else "upstream_contract", field
                        )
                    if status != 200:
                        raise DependencyError(
                            "upstream_contract"
                            if 300 <= status < 400
                            else "upstream_http"
                        )
                    outcome = "success"
                    return payload
            except (
                httpx.ConnectError,
                httpx.ReadError,
                httpx.WriteError,
                httpx.RemoteProtocolError,
                httpx.TimeoutException,
                TimeoutError,
            ) as exc:
                cause = exc
                while cause is not None:
                    if isinstance(cause, ssl.SSLError):
                        raise DependencyError("upstream_tls") from None
                    cause = cause.__cause__ or cause.__context__
                continue_retry = True
            except httpx.HTTPError:
                raise DependencyError("upstream_contract") from None
            finally:
                self.event(
                    "upstream_attempt",
                    correlation_id=correlation_id,
                    attempt=attempt,
                    elapsed_ms=round((self.clock() - start) * 1000),
                    upstream_status=status,
                    outcome=outcome,
                )
            if continue_retry and attempt < self.settings.max_attempts:
                remaining = deadline - self.clock()
                if remaining <= 0:
                    break
                await self.sleep(
                    min(self.jitter(0, 0.2 * 2 ** (attempt - 1)), remaining)
                )
        raise DependencyError()

    async def catalogue(self, deadline, cid):
        payload = await self.operation("GET", "/planos", deadline, cid)
        try:
            return Catalogue.model_validate(payload)
        except (ValidationError, ValueError):
            raise DependencyError("upstream_contract") from None

    async def quote(self, inputs, catalogue, deadline, cid):
        payload = await self.operation("POST", "/quote", deadline, cid, json=inputs)
        try:
            return QuoteResult.model_validate(payload).check_request(inputs, catalogue)
        except (ValidationError, ValueError):
            raise DependencyError("upstream_contract") from None
