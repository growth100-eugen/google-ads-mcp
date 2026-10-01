# Growth100 creation tools for the Google Ads MCP server.
#
# Same rule as mutate.py: confirm=False (default) is a validate-only dry run
# that returns a preview and creates nothing. confirm=True creates and logs.

"""Create search campaigns, ad groups and responsive search ads."""

from typing import Any, Dict, List, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException
from mcp.types import ToolAnnotations

import ads_mcp.utils as utils
from ads_mcp.mcp_header_interceptor import MCPHeaderInterceptor

create_mcp = FastMCP("create")

_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)

_NEXT_STEP = (
    "Nothing was created. Show this preview to the person and, only after an "
    "explicit yes, call the same tool again with confirm=true."
)

Status = Literal["ENABLED", "PAUSED"]
Bidding = Literal["MAXIMIZE_CONVERSIONS", "MAXIMIZE_CLICKS", "MANUAL_CPC"]

_DEFAULT_LANGUAGE_ID = "1000"  # English
_DEFAULT_GEO_IDS = ["2840"]  # United States

_HEADLINE_MAX = 30
_DESCRIPTION_MAX = 90
_PATH_MAX = 15


def _client(login_customer_id):
    return utils.get_googleads_client(login_customer_id=login_customer_id)


def _service(client, name):
    return client.get_service(name, interceptors=[MCPHeaderInterceptor()])


def _micros(amount: float) -> int:
    return int(round(float(amount) * 1_000_000))


def _raise(ex: GoogleAdsException) -> None:
    msgs = [f"Google Ads API Error: {e.message}" for e in ex.failure.errors]
    raise ToolError(f"Request ID: {ex.request_id}\n" + "\n".join(msgs))


def _query(client, customer_id: str, gaql: str) -> List[Dict[str, Any]]:
    svc = _service(client, "GoogleAdsService")
    out: List[Dict[str, Any]] = []
    try:
        for batch in svc.search_stream(customer_id=customer_id, query=gaql):
            for row in batch.results:
                out.append(utils.format_output_row(row, batch.field_mask.paths))
    except GoogleAdsException as ex:
        _raise(ex)
    return out


def _run_mutate(client, customer_id: str, operations, confirm: bool, preview, label):
    """Runs a MutateGoogleAdsRequest (atomic across resources)."""
    svc = _service(client, "GoogleAdsService")
    req = client.get_type("MutateGoogleAdsRequest")
    req.customer_id = customer_id
    req.mutate_operations.extend(operations)
    try:
        if not confirm:
            req.validate_only = True
            svc.mutate(request=req)
            return {
                "created": False,
                "validated_by_api": True,
                "plan": preview,
                "next_step": _NEXT_STEP,
            }
        resp = svc.mutate(request=req)
    except GoogleAdsException as ex:
        _raise(ex)

    names: List[str] = []
    for r in resp.mutate_operation_responses:
        for field in (
            "campaign_budget_result",
            "campaign_result",
            "campaign_criterion_result",
            "ad_group_result",
            "ad_group_ad_result",
        ):
            rn = getattr(getattr(r, field), "resource_name", "")
            if rn:
                names.append(rn)
    utils.logger.info("ads_mcp.create CREATED %s %s -> %s", label, preview, names)
    return {"created": True, "plan": preview, "resource_names": names}


# ----------------------------------------------------------------------------
# campaign
# ----------------------------------------------------------------------------


@create_mcp.tool(annotations=_WRITE)
def create_search_campaign(
    customer_id: str | int,
    name: str,
    daily_budget: float,
    bidding: Bidding = "MAXIMIZE_CONVERSIONS",
    target_cpa: float | None = None,
    max_cpc_ceiling: float | None = None,
    geo_target_ids: List[str | int] | None = None,
    language_id: str | int = _DEFAULT_LANGUAGE_ID,
    include_search_partners: bool = False,
    status: Status = "PAUSED",
    confirm: bool = False,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Create a Search campaign with its own budget, location and language.

    Creates the budget, the campaign, and the location/language criteria in
    one atomic request. Google Search only, no Display, no partners unless
    include_search_partners is true. Defaults to PAUSED so ads and keywords
    can be added before anything serves.

    bidding: MAXIMIZE_CONVERSIONS (optional target_cpa), MAXIMIZE_CLICKS
    (optional max_cpc_ceiling), or MANUAL_CPC.
    geo_target_ids: from planner_suggest_geo_targets; default United States.
    Dry run by default; confirm=true creates.
    """
    customer_id = utils.clean_customer_id(customer_id)
    if daily_budget <= 0:
        raise ToolError("daily_budget must be greater than zero.")
    client = _client(login_customer_id)

    dup = _query(
        client,
        customer_id,
        "SELECT campaign.id, campaign.name, campaign.status FROM campaign "
        f"WHERE campaign.name = '{name.replace(chr(39), chr(39)*2)}' "
        "AND campaign.status != 'REMOVED'",
    )
    if dup:
        raise ToolError(f"A campaign named '{name}' already exists ({dup[0]['campaign.id']}).")

    geo_ids = [str(g) for g in (geo_target_ids or _DEFAULT_GEO_IDS)]
    budget_rn = f"customers/{customer_id}/campaignBudgets/-1"
    campaign_rn = f"customers/{customer_id}/campaigns/-2"
    ops = []

    # 1. budget
    op = client.get_type("MutateOperation")
    b = op.campaign_budget_operation.create
    b.resource_name = budget_rn
    b.name = f"{name} budget"
    b.amount_micros = _micros(daily_budget)
    b.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
    b.explicitly_shared = False
    ops.append(op)

    # 2. campaign
    op = client.get_type("MutateOperation")
    c = op.campaign_operation.create
    c.resource_name = campaign_rn
    c.name = name
    c.status = getattr(client.enums.CampaignStatusEnum, status)
    c.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.SEARCH
    c.campaign_budget = budget_rn
    c.contains_eu_political_advertising = (
        client.enums.EuPoliticalAdvertisingStatusEnum.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
    )
    ns = c.network_settings
    ns.target_google_search = True
    ns.target_search_network = include_search_partners
    ns.target_content_network = False
    ns.target_partner_search_network = False

    if bidding == "MAXIMIZE_CONVERSIONS":
        if target_cpa is not None:
            c.maximize_conversions.target_cpa_micros = _micros(target_cpa)
        else:
            c.maximize_conversions.target_cpa_micros = 0
    elif bidding == "MAXIMIZE_CLICKS":
        if max_cpc_ceiling is not None:
            c.target_spend.cpc_bid_ceiling_micros = _micros(max_cpc_ceiling)
        else:
            c.target_spend.cpc_bid_ceiling_micros = 0
    else:
        c.manual_cpc.enhanced_cpc_enabled = False
    ops.append(op)

    # 3. locations
    geo_svc = _service(client, "GeoTargetConstantService")
    for g in geo_ids:
        op = client.get_type("MutateOperation")
        cc = op.campaign_criterion_operation.create
        cc.campaign = campaign_rn
        cc.location.geo_target_constant = geo_svc.geo_target_constant_path(g)
        ops.append(op)

    # 4. language
    op = client.get_type("MutateOperation")
    cc = op.campaign_criterion_operation.create
    cc.campaign = campaign_rn
    cc.language.language_constant = _service(
        client, "GoogleAdsService"
    ).language_constant_path(str(language_id))
    ops.append(op)

    preview = {
        "action": "create_search_campaign",
        "name": name,
        "status": status,
        "daily_budget": f"{daily_budget:,.2f}",
        "bidding": bidding,
        "target_cpa": target_cpa,
        "max_cpc_ceiling": max_cpc_ceiling,
        "network": "Google Search" + (" + search partners" if include_search_partners else ""),
        "locations": geo_ids,
        "language_id": str(language_id),
        "note": "Add ad groups, keywords and at least one RSA before enabling.",
    }
    return _run_mutate(client, customer_id, ops, confirm, preview, "campaign")


# ----------------------------------------------------------------------------
# ad group
# ----------------------------------------------------------------------------


@create_mcp.tool(annotations=_WRITE)
def create_ad_group(
    customer_id: str | int,
    campaign_id: str | int,
    name: str,
    cpc_bid: float | None = None,
    status: Status = "ENABLED",
    confirm: bool = False,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Create a standard Search ad group in a campaign.

    cpc_bid is only used by manual CPC campaigns. Ad group status defaults
    to ENABLED because the campaign's own status gates serving.
    Dry run by default; confirm=true creates.
    """
    customer_id = utils.clean_customer_id(customer_id)
    client = _client(login_customer_id)

    camp = _query(
        client,
        customer_id,
        "SELECT campaign.id, campaign.name, campaign.status, "
        "campaign.advertising_channel_type FROM campaign "
        f"WHERE campaign.id = {int(campaign_id)}",
    )
    if not camp:
        raise ToolError(f"Campaign {campaign_id} not found in {customer_id}.")
    dup = _query(
        client,
        customer_id,
        "SELECT ad_group.id FROM ad_group "
        f"WHERE campaign.id = {int(campaign_id)} "
        f"AND ad_group.name = '{name.replace(chr(39), chr(39)*2)}' "
        "AND ad_group.status != 'REMOVED'",
    )
    if dup:
        raise ToolError(f"Ad group '{name}' already exists in that campaign ({dup[0]['ad_group.id']}).")

    op = client.get_type("MutateOperation")
    ag = op.ad_group_operation.create
    ag.name = name
    ag.campaign = _service(client, "CampaignService").campaign_path(customer_id, campaign_id)
    ag.status = getattr(client.enums.AdGroupStatusEnum, status)
    ag.type_ = client.enums.AdGroupTypeEnum.SEARCH_STANDARD
    if cpc_bid is not None:
        ag.cpc_bid_micros = _micros(cpc_bid)

    preview = {
        "action": "create_ad_group",
        "campaign": f"{camp[0]['campaign.name']} ({campaign_id})",
        "name": name,
        "status": status,
        "cpc_bid": f"{cpc_bid:,.2f}" if cpc_bid is not None else "campaign bidding",
    }
    return _run_mutate(client, customer_id, [op], confirm, preview, "ad group")


# ----------------------------------------------------------------------------
# responsive search ad
# ----------------------------------------------------------------------------


@create_mcp.tool(annotations=_WRITE)
def create_responsive_search_ad(
    customer_id: str | int,
    ad_group_id: str | int,
    final_url: str,
    headlines: List[str],
    descriptions: List[str],
    path1: str | None = None,
    path2: str | None = None,
    pin_first_headline: bool = False,
    status: Status = "ENABLED",
    confirm: bool = False,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Create a responsive search ad in an ad group.

    headlines: 3 to 15, each 30 characters or fewer.
    descriptions: 2 to 4, each 90 characters or fewer.
    path1/path2: optional display URL paths, 15 characters each.
    pin_first_headline pins headlines[0] to position 1.
    The dry run checks lengths locally and runs Google's policy validation.
    Dry run by default; confirm=true creates.
    """
    customer_id = utils.clean_customer_id(customer_id)
    headlines = [h.strip() for h in headlines if h and h.strip()]
    descriptions = [d.strip() for d in descriptions if d and d.strip()]

    problems: List[str] = []
    if not 3 <= len(headlines) <= 15:
        problems.append(f"headlines: need 3 to 15, got {len(headlines)}")
    if not 2 <= len(descriptions) <= 4:
        problems.append(f"descriptions: need 2 to 4, got {len(descriptions)}")
    for h in headlines:
        if len(h) > _HEADLINE_MAX:
            problems.append(f"headline over {_HEADLINE_MAX} chars ({len(h)}): {h}")
    for d in descriptions:
        if len(d) > _DESCRIPTION_MAX:
            problems.append(f"description over {_DESCRIPTION_MAX} chars ({len(d)}): {d}")
    for p in (path1, path2):
        if p and len(p) > _PATH_MAX:
            problems.append(f"path over {_PATH_MAX} chars: {p}")
    if len(set(h.lower() for h in headlines)) != len(headlines):
        problems.append("duplicate headlines")
    if not final_url.startswith(("http://", "https://")):
        problems.append("final_url must start with http:// or https://")
    if problems:
        raise ToolError("Fix before submitting:\n- " + "\n- ".join(problems))

    client = _client(login_customer_id)
    ag = _query(
        client,
        customer_id,
        "SELECT ad_group.id, ad_group.name, campaign.name FROM ad_group "
        f"WHERE ad_group.id = {int(ad_group_id)}",
    )
    if not ag:
        raise ToolError(f"Ad group {ad_group_id} not found in {customer_id}.")

    op = client.get_type("MutateOperation")
    aga = op.ad_group_ad_operation.create
    aga.ad_group = _service(client, "AdGroupService").ad_group_path(customer_id, ad_group_id)
    aga.status = getattr(client.enums.AdGroupAdStatusEnum, status)
    ad = aga.ad
    ad.final_urls.append(final_url)
    rsa = ad.responsive_search_ad
    for i, h in enumerate(headlines):
        asset = client.get_type("AdTextAsset")
        asset.text = h
        if i == 0 and pin_first_headline:
            asset.pinned_field = client.enums.ServedAssetFieldTypeEnum.HEADLINE_1
        rsa.headlines.append(asset)
    for d in descriptions:
        asset = client.get_type("AdTextAsset")
        asset.text = d
        rsa.descriptions.append(asset)
    if path1:
        rsa.path1 = path1
    if path2:
        rsa.path2 = path2

    preview = {
        "action": "create_responsive_search_ad",
        "ad_group": f"{ag[0]['ad_group.name']} ({ad_group_id})",
        "campaign": ag[0]["campaign.name"],
        "status": status,
        "final_url": final_url,
        "display_path": "/".join(p for p in (path1, path2) if p) or None,
        "headlines": [f"{h} ({len(h)})" for h in headlines],
        "descriptions": [f"{d} ({len(d)})" for d in descriptions],
        "pinned": "headline 1" if pin_first_headline else None,
    }
    return _run_mutate(client, customer_id, [op], confirm, preview, "rsa")
