# Growth100 keyword planner tools for the Google Ads MCP server.
#
# All read-only. Wraps KeywordPlanIdeaService (ideas, historical metrics,
# forecasts) and GeoTargetConstantService (location lookup).

"""Keyword planner: ideas, search volume, forecasts, geo lookup."""

from datetime import date, timedelta
from typing import Any, Dict, List, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException
from mcp.types import ToolAnnotations

import ads_mcp.utils as utils
from ads_mcp.mcp_header_interceptor import MCPHeaderInterceptor

planner_mcp = FastMCP("planner")

_READ = ToolAnnotations(readOnlyHint=True)

# English, United States. Override per call.
_DEFAULT_LANGUAGE_ID = "1000"
_DEFAULT_GEO_IDS = ["2840"]

MatchType = Literal["EXACT", "PHRASE", "BROAD"]
Network = Literal["GOOGLE_SEARCH", "GOOGLE_SEARCH_AND_PARTNERS"]


def _client(login_customer_id):
    return utils.get_googleads_client(login_customer_id=login_customer_id)


def _service(client, name):
    return client.get_service(name, interceptors=[MCPHeaderInterceptor()])


def _raise(ex: GoogleAdsException) -> None:
    msgs = [f"Google Ads API Error: {e.message}" for e in ex.failure.errors]
    raise ToolError(f"Request ID: {ex.request_id}\n" + "\n".join(msgs))


def _money(micros) -> float | None:
    return None if micros in (None, 0) else round(micros / 1_000_000, 2)


def _metrics_to_dict(m) -> Dict[str, Any]:
    return {
        "avg_monthly_searches": m.avg_monthly_searches,
        "competition": m.competition.name if m.competition else None,
        "competition_index": m.competition_index,
        "low_top_of_page_bid": _money(m.low_top_of_page_bid_micros),
        "high_top_of_page_bid": _money(m.high_top_of_page_bid_micros),
        "monthly_search_volumes": [
            {"year": v.year, "month": v.month.name, "searches": v.monthly_searches}
            for v in m.monthly_search_volumes
        ][-12:],
    }


def _language_rn(client, language_id) -> str:
    return _service(client, "GoogleAdsService").language_constant_path(language_id)


def _geo_rns(client, geo_target_ids) -> List[str]:
    svc = _service(client, "GoogleAdsService")
    return [svc.geo_target_constant_path(str(g)) for g in geo_target_ids]


@planner_mcp.tool(annotations=_READ)
def suggest_geo_targets(
    location_names: List[str],
    country_code: str = "US",
    login_customer_id: str | int | None = None,
) -> List[Dict[str, Any]]:
    """Look up geo target IDs for location names (city, state, metro).

    Use the returned ids as geo_target_ids in the other planner tools.
    Example: ["Phoenix", "Arizona"].
    """
    client = _client(login_customer_id)
    svc = _service(client, "GeoTargetConstantService")
    req = client.get_type("SuggestGeoTargetConstantsRequest")
    req.locale = "en"
    req.country_code = country_code
    req.location_names.names.extend(location_names)
    try:
        resp = svc.suggest_geo_target_constants(request=req)
    except GoogleAdsException as ex:
        _raise(ex)
    return [
        {
            "id": s.geo_target_constant.id,
            "name": s.geo_target_constant.name,
            "canonical_name": s.geo_target_constant.canonical_name,
            "type": s.geo_target_constant.target_type,
            "reach": s.reach,
            "searched_for": s.search_term,
        }
        for s in resp.geo_target_constant_suggestions
    ]


@planner_mcp.tool(annotations=_READ)
def generate_keyword_ideas(
    customer_id: str | int,
    seed_keywords: List[str] | None = None,
    seed_url: str | None = None,
    geo_target_ids: List[str | int] | None = None,
    language_id: str | int = _DEFAULT_LANGUAGE_ID,
    network: Network = "GOOGLE_SEARCH",
    limit: int = 50,
    login_customer_id: str | int | None = None,
) -> List[Dict[str, Any]]:
    """Discover new keyword ideas from seed keywords and/or a page URL.

    Returns each idea with average monthly searches, competition and top of
    page bid range. Use suggest_geo_targets to get geo_target_ids for a city
    or state; default is United States.
    """
    customer_id = utils.clean_customer_id(customer_id)
    if not seed_keywords and not seed_url:
        raise ToolError("Give seed_keywords, seed_url, or both.")
    client = _client(login_customer_id)
    svc = _service(client, "KeywordPlanIdeaService")

    req = client.get_type("GenerateKeywordIdeasRequest")
    req.customer_id = customer_id
    req.language = _language_rn(client, language_id)
    req.geo_target_constants.extend(_geo_rns(client, geo_target_ids or _DEFAULT_GEO_IDS))
    req.include_adult_keywords = False
    req.keyword_plan_network = getattr(client.enums.KeywordPlanNetworkEnum, network)
    if seed_keywords and seed_url:
        req.keyword_and_url_seed.url = seed_url
        req.keyword_and_url_seed.keywords.extend(seed_keywords)
    elif seed_keywords:
        req.keyword_seed.keywords.extend(seed_keywords)
    else:
        req.url_seed.url = seed_url

    try:
        resp = svc.generate_keyword_ideas(request=req)
    except GoogleAdsException as ex:
        _raise(ex)

    out: List[Dict[str, Any]] = []
    for idea in resp:
        out.append({"keyword": idea.text, **_metrics_to_dict(idea.keyword_idea_metrics)})
        if len(out) >= limit:
            break
    return out


@planner_mcp.tool(annotations=_READ)
def get_keyword_metrics(
    customer_id: str | int,
    keywords: List[str],
    geo_target_ids: List[str | int] | None = None,
    language_id: str | int = _DEFAULT_LANGUAGE_ID,
    network: Network = "GOOGLE_SEARCH",
    login_customer_id: str | int | None = None,
) -> List[Dict[str, Any]]:
    """Historical search volume and bid range for specific keywords.

    Returns the last 12 months of monthly searches per keyword. Up to a few
    hundred keywords per call.
    """
    customer_id = utils.clean_customer_id(customer_id)
    keywords = [k.strip() for k in keywords if k and k.strip()]
    if not keywords:
        raise ToolError("keywords is empty.")
    client = _client(login_customer_id)
    svc = _service(client, "KeywordPlanIdeaService")

    req = client.get_type("GenerateKeywordHistoricalMetricsRequest")
    req.customer_id = customer_id
    req.keywords.extend(keywords)
    req.language = _language_rn(client, language_id)
    req.geo_target_constants.extend(_geo_rns(client, geo_target_ids or _DEFAULT_GEO_IDS))
    req.include_adult_keywords = False
    req.keyword_plan_network = getattr(client.enums.KeywordPlanNetworkEnum, network)

    try:
        resp = svc.generate_keyword_historical_metrics(request=req)
    except GoogleAdsException as ex:
        _raise(ex)

    return [
        {
            "keyword": r.text,
            "close_variants": list(r.close_variants),
            **_metrics_to_dict(r.keyword_metrics),
        }
        for r in resp.results
    ]


@planner_mcp.tool(annotations=_READ)
def get_keyword_forecast(
    customer_id: str | int,
    keywords: List[str],
    match_type: MatchType = "PHRASE",
    max_cpc_bid: float | None = None,
    daily_budget: float | None = None,
    geo_target_ids: List[str | int] | None = None,
    language_id: str | int = _DEFAULT_LANGUAGE_ID,
    days: int = 30,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Forecast clicks, impressions, cost and conversions for a keyword set.

    Models a single-ad-group campaign. With max_cpc_bid it uses manual CPC at
    that bid; otherwise it uses maximize clicks, with daily_budget as the cap
    if given. Forecast window starts tomorrow and runs for `days`.
    """
    customer_id = utils.clean_customer_id(customer_id)
    keywords = [k.strip() for k in keywords if k and k.strip()]
    if not keywords:
        raise ToolError("keywords is empty.")
    client = _client(login_customer_id)
    svc = _service(client, "KeywordPlanIdeaService")

    req = client.get_type("GenerateKeywordForecastMetricsRequest")
    req.customer_id = customer_id
    camp = req.campaign
    camp.language_constants.append(_language_rn(client, language_id))
    camp.geo_target_constants.extend(
        _geo_rns(client, geo_target_ids or _DEFAULT_GEO_IDS)
    )

    if max_cpc_bid is not None:
        manual = camp.bidding_strategy.manual_cpc_bidding_strategy
        manual.max_cpc_bid_micros = int(round(max_cpc_bid * 1_000_000))
        if daily_budget is not None:
            manual.daily_budget_micros = int(round(daily_budget * 1_000_000))
    else:
        mc = camp.bidding_strategy.maximize_clicks_bidding_strategy
        if daily_budget is not None:
            mc.daily_target_spend_micros = int(round(daily_budget * 1_000_000))

    ag = client.get_type("ForecastAdGroup")
    mt = getattr(client.enums.KeywordMatchTypeEnum, match_type)
    for kw in keywords:
        ki = client.get_type("KeywordInfo")
        ki.text = kw
        ki.match_type = mt
        ag.keywords.append(ki)
    camp.ad_groups.append(ag)

    start = date.today() + timedelta(days=1)
    end = start + timedelta(days=max(1, days) - 1)
    req.forecast_period.start_date = start.isoformat()
    req.forecast_period.end_date = end.isoformat()

    try:
        resp = svc.generate_keyword_forecast_metrics(request=req)
    except GoogleAdsException as ex:
        _raise(ex)

    m = resp.campaign_forecast_metrics
    cost = _money(m.cost_micros)
    return {
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "keywords": keywords,
        "match_type": match_type,
        "bidding": "manual_cpc" if max_cpc_bid is not None else "maximize_clicks",
        "clicks": round(m.clicks, 1),
        "average_cpc": _money(m.average_cpc_micros),
        "cost": cost,
        "conversions": round(m.conversions, 2),
        "average_cpa": _money(m.average_cpa_micros),
    }
