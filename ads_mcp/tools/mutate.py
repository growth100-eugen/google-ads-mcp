# Growth100 write layer for the Google Ads MCP server.
#
# Every tool here changes a live account, so every tool follows one rule:
#
#   confirm=False (the default)  ->  validate-only dry run against the API,
#                                    returns a human-readable preview, applies
#                                    NOTHING.
#   confirm=True                 ->  applies the change and logs it.
#
# The MCP client (Claude) is expected to show the preview to a person and only
# call again with confirm=True after an explicit yes. The server enforces the
# dry run; the person enforces the yes.

"""Write tools: campaign/ad group status, budgets, keywords, negatives."""

from typing import Any, Dict, List, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException
from google.api_core import protobuf_helpers
from mcp.types import ToolAnnotations

import ads_mcp.utils as utils
from ads_mcp.mcp_header_interceptor import MCPHeaderInterceptor

mutate_mcp = FastMCP("mutate")

_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)

Status = Literal["ENABLED", "PAUSED"]
KeywordStatus = Literal["ENABLED", "PAUSED", "REMOVED"]
MatchType = Literal["EXACT", "PHRASE", "BROAD"]

_NEXT_STEP = (
    "Nothing was changed. Show this preview to the person and, only after an "
    "explicit yes, call the same tool again with confirm=true."
)


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------


def _client(login_customer_id):
    return utils.get_googleads_client(login_customer_id=login_customer_id)


def _service(client, name):
    return client.get_service(name, interceptors=[MCPHeaderInterceptor()])


def _micros(amount: float) -> int:
    return int(round(float(amount) * 1_000_000))


def _money(micros: int | None) -> str:
    if micros is None:
        return "n/a"
    return f"{micros / 1_000_000:,.2f}"


def _raise(ex: GoogleAdsException) -> None:
    msgs = [f"Google Ads API Error: {e.message}" for e in ex.failure.errors]
    raise ToolError(f"Request ID: {ex.request_id}\n" + "\n".join(msgs))


def _query(client, customer_id: str, gaql: str) -> List[Dict[str, Any]]:
    """Small GAQL helper used to build previews with current state."""
    svc = _service(client, "GoogleAdsService")
    out: List[Dict[str, Any]] = []
    try:
        for batch in svc.search_stream(customer_id=customer_id, query=gaql):
            for row in batch.results:
                out.append(utils.format_output_row(row, batch.field_mask.paths))
    except GoogleAdsException as ex:
        _raise(ex)
    return out


def _execute(
    service,
    method: str,
    request,
    confirm: bool,
    preview: Dict[str, Any],
    label: str,
) -> Dict[str, Any]:
    """Runs a mutate request as a dry run, or applies it when confirmed."""
    try:
        if not confirm:
            request.validate_only = True
            getattr(service, method)(request=request)
            return {
                "applied": False,
                "validated_by_api": True,
                "change": preview,
                "next_step": _NEXT_STEP,
            }
        response = getattr(service, method)(request=request)
    except GoogleAdsException as ex:
        _raise(ex)

    results = [r.resource_name for r in response.results]
    utils.logger.info("ads_mcp.mutate APPLIED %s %s -> %s", label, preview, results)
    return {"applied": True, "change": preview, "resource_names": results}


def _set_mask(client, operation, message) -> None:
    client.copy_from(
        operation.update_mask, protobuf_helpers.field_mask(None, message._pb)
    )


# ----------------------------------------------------------------------------
# campaign / ad group status
# ----------------------------------------------------------------------------


@mutate_mcp.tool(annotations=_WRITE)
def set_campaign_status(
    customer_id: str | int,
    campaign_id: str | int,
    status: Status,
    confirm: bool = False,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Pause or enable a campaign.

    Default is a dry run: validates against the API and returns a preview with
    the campaign's current status. Call again with confirm=true to apply.

    Args:
        customer_id: Client account ID (digits only).
        campaign_id: The campaign ID.
        status: "ENABLED" or "PAUSED".
        confirm: False = preview only. True = apply.
        login_customer_id: Optional manager account ID.
    """
    customer_id = utils.clean_customer_id(customer_id)
    client = _client(login_customer_id)

    rows = _query(
        client,
        customer_id,
        f"SELECT campaign.id, campaign.name, campaign.status FROM campaign "
        f"WHERE campaign.id = {int(campaign_id)}",
    )
    if not rows:
        raise ToolError(f"Campaign {campaign_id} not found in {customer_id}.")
    current = rows[0]

    svc = _service(client, "CampaignService")
    op = client.get_type("CampaignOperation")
    camp = op.update
    camp.resource_name = svc.campaign_path(customer_id, campaign_id)
    camp.status = getattr(client.enums.CampaignStatusEnum, status)
    _set_mask(client, op, camp)

    req = client.get_type("MutateCampaignsRequest")
    req.customer_id = customer_id
    req.operations.append(op)

    preview = {
        "action": "set_campaign_status",
        "campaign": f"{current['campaign.name']} ({campaign_id})",
        "from": current["campaign.status"],
        "to": status,
    }
    return _execute(svc, "mutate_campaigns", req, confirm, preview, "campaign status")


@mutate_mcp.tool(annotations=_WRITE)
def set_ad_group_status(
    customer_id: str | int,
    ad_group_id: str | int,
    status: Status,
    confirm: bool = False,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Pause or enable an ad group. Dry run by default; confirm=true applies."""
    customer_id = utils.clean_customer_id(customer_id)
    client = _client(login_customer_id)

    rows = _query(
        client,
        customer_id,
        f"SELECT ad_group.id, ad_group.name, ad_group.status, campaign.name "
        f"FROM ad_group WHERE ad_group.id = {int(ad_group_id)}",
    )
    if not rows:
        raise ToolError(f"Ad group {ad_group_id} not found in {customer_id}.")
    current = rows[0]

    svc = _service(client, "AdGroupService")
    op = client.get_type("AdGroupOperation")
    ag = op.update
    ag.resource_name = svc.ad_group_path(customer_id, ad_group_id)
    ag.status = getattr(client.enums.AdGroupStatusEnum, status)
    _set_mask(client, op, ag)

    req = client.get_type("MutateAdGroupsRequest")
    req.customer_id = customer_id
    req.operations.append(op)

    preview = {
        "action": "set_ad_group_status",
        "ad_group": f"{current['ad_group.name']} ({ad_group_id})",
        "campaign": current["campaign.name"],
        "from": current["ad_group.status"],
        "to": status,
    }
    return _execute(svc, "mutate_ad_groups", req, confirm, preview, "ad group status")


# ----------------------------------------------------------------------------
# budgets
# ----------------------------------------------------------------------------


@mutate_mcp.tool(annotations=_WRITE)
def set_campaign_daily_budget(
    customer_id: str | int,
    campaign_id: str | int,
    daily_budget: float,
    confirm: bool = False,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Change a campaign's daily budget, in the account's currency units.

    Dry run by default; the preview shows the current budget. confirm=true
    applies. Refuses if the budget is shared by more than one campaign.
    """
    customer_id = utils.clean_customer_id(customer_id)
    if daily_budget <= 0:
        raise ToolError("daily_budget must be greater than zero.")
    client = _client(login_customer_id)

    rows = _query(
        client,
        customer_id,
        "SELECT campaign.id, campaign.name, campaign.campaign_budget, "
        "campaign_budget.amount_micros, campaign_budget.explicitly_shared, "
        "campaign_budget.reference_count "
        f"FROM campaign WHERE campaign.id = {int(campaign_id)}",
    )
    if not rows:
        raise ToolError(f"Campaign {campaign_id} not found in {customer_id}.")
    current = rows[0]
    if current.get("campaign_budget.reference_count", 1) > 1:
        raise ToolError(
            "This budget is shared by more than one campaign. Change it in the "
            "Google Ads UI so the other campaigns are reviewed too."
        )

    svc = _service(client, "CampaignBudgetService")
    op = client.get_type("CampaignBudgetOperation")
    budget = op.update
    budget.resource_name = current["campaign.campaign_budget"]
    budget.amount_micros = _micros(daily_budget)
    _set_mask(client, op, budget)

    req = client.get_type("MutateCampaignBudgetsRequest")
    req.customer_id = customer_id
    req.operations.append(op)

    preview = {
        "action": "set_campaign_daily_budget",
        "campaign": f"{current['campaign.name']} ({campaign_id})",
        "from_daily": _money(current.get("campaign_budget.amount_micros")),
        "to_daily": _money(budget.amount_micros),
    }
    return _execute(svc, "mutate_campaign_budgets", req, confirm, preview, "budget")


# ----------------------------------------------------------------------------
# negatives
# ----------------------------------------------------------------------------


@mutate_mcp.tool(annotations=_WRITE)
def add_campaign_negative_keywords(
    customer_id: str | int,
    campaign_id: str | int,
    keywords: List[str],
    match_type: MatchType = "PHRASE",
    confirm: bool = False,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Add negative keywords at the campaign level.

    Dry run by default; the API validates every keyword. confirm=true applies.
    Duplicates of existing negatives are rejected by the API and reported.
    """
    customer_id = utils.clean_customer_id(customer_id)
    keywords = [k.strip() for k in keywords if k and k.strip()]
    if not keywords:
        raise ToolError("keywords is empty.")
    client = _client(login_customer_id)

    rows = _query(
        client,
        customer_id,
        f"SELECT campaign.id, campaign.name FROM campaign "
        f"WHERE campaign.id = {int(campaign_id)}",
    )
    if not rows:
        raise ToolError(f"Campaign {campaign_id} not found in {customer_id}.")

    svc = _service(client, "CampaignCriterionService")
    campaign_rn = _service(client, "CampaignService").campaign_path(
        customer_id, campaign_id
    )
    mt = getattr(client.enums.KeywordMatchTypeEnum, match_type)

    req = client.get_type("MutateCampaignCriteriaRequest")
    req.customer_id = customer_id
    for kw in keywords:
        op = client.get_type("CampaignCriterionOperation")
        crit = op.create
        crit.campaign = campaign_rn
        crit.negative = True
        crit.keyword.text = kw
        crit.keyword.match_type = mt
        req.operations.append(op)

    preview = {
        "action": "add_campaign_negative_keywords",
        "campaign": f"{rows[0]['campaign.name']} ({campaign_id})",
        "match_type": match_type,
        "count": len(keywords),
        "keywords": keywords,
    }
    return _execute(
        svc, "mutate_campaign_criteria", req, confirm, preview, "negatives"
    )


@mutate_mcp.tool(annotations=_WRITE)
def remove_campaign_criterion(
    customer_id: str | int,
    campaign_id: str | int,
    criterion_id: str | int,
    confirm: bool = False,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Remove one campaign-level criterion (for example a negative keyword).

    Find criterion_id with the search tool on campaign_criterion. Dry run by
    default; confirm=true applies.
    """
    customer_id = utils.clean_customer_id(customer_id)
    client = _client(login_customer_id)

    rows = _query(
        client,
        customer_id,
        "SELECT campaign.name, campaign_criterion.criterion_id, "
        "campaign_criterion.negative, campaign_criterion.keyword.text, "
        "campaign_criterion.keyword.match_type, campaign_criterion.type "
        f"FROM campaign_criterion WHERE campaign.id = {int(campaign_id)} "
        f"AND campaign_criterion.criterion_id = {int(criterion_id)}",
    )
    if not rows:
        raise ToolError(f"Criterion {criterion_id} not found on campaign {campaign_id}.")
    current = rows[0]

    svc = _service(client, "CampaignCriterionService")
    op = client.get_type("CampaignCriterionOperation")
    op.remove = svc.campaign_criterion_path(customer_id, campaign_id, criterion_id)

    req = client.get_type("MutateCampaignCriteriaRequest")
    req.customer_id = customer_id
    req.operations.append(op)

    preview = {
        "action": "remove_campaign_criterion",
        "campaign": f"{current['campaign.name']} ({campaign_id})",
        "criterion_id": str(criterion_id),
        "type": current.get("campaign_criterion.type"),
        "negative": current.get("campaign_criterion.negative"),
        "keyword": current.get("campaign_criterion.keyword.text"),
        "match_type": current.get("campaign_criterion.keyword.match_type"),
    }
    return _execute(
        svc, "mutate_campaign_criteria", req, confirm, preview, "remove criterion"
    )


# ----------------------------------------------------------------------------
# keywords
# ----------------------------------------------------------------------------


@mutate_mcp.tool(annotations=_WRITE)
def add_ad_group_keywords(
    customer_id: str | int,
    ad_group_id: str | int,
    keywords: List[str],
    match_type: MatchType = "PHRASE",
    cpc_bid: float | None = None,
    confirm: bool = False,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Add keywords to an ad group, enabled, with an optional max CPC bid.

    Dry run by default; the API validates every keyword against policy.
    confirm=true applies.
    """
    customer_id = utils.clean_customer_id(customer_id)
    keywords = [k.strip() for k in keywords if k and k.strip()]
    if not keywords:
        raise ToolError("keywords is empty.")
    client = _client(login_customer_id)

    rows = _query(
        client,
        customer_id,
        f"SELECT ad_group.id, ad_group.name, campaign.name FROM ad_group "
        f"WHERE ad_group.id = {int(ad_group_id)}",
    )
    if not rows:
        raise ToolError(f"Ad group {ad_group_id} not found in {customer_id}.")

    svc = _service(client, "AdGroupCriterionService")
    ad_group_rn = _service(client, "AdGroupService").ad_group_path(
        customer_id, ad_group_id
    )
    mt = getattr(client.enums.KeywordMatchTypeEnum, match_type)

    req = client.get_type("MutateAdGroupCriteriaRequest")
    req.customer_id = customer_id
    for kw in keywords:
        op = client.get_type("AdGroupCriterionOperation")
        crit = op.create
        crit.ad_group = ad_group_rn
        crit.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
        crit.keyword.text = kw
        crit.keyword.match_type = mt
        if cpc_bid is not None:
            crit.cpc_bid_micros = _micros(cpc_bid)
        req.operations.append(op)

    preview = {
        "action": "add_ad_group_keywords",
        "ad_group": f"{rows[0]['ad_group.name']} ({ad_group_id})",
        "campaign": rows[0]["campaign.name"],
        "match_type": match_type,
        "cpc_bid": _money(_micros(cpc_bid)) if cpc_bid is not None else "ad group default",
        "count": len(keywords),
        "keywords": keywords,
    }
    return _execute(
        svc, "mutate_ad_group_criteria", req, confirm, preview, "add keywords"
    )


@mutate_mcp.tool(annotations=_WRITE)
def set_keyword_status(
    customer_id: str | int,
    ad_group_id: str | int,
    criterion_id: str | int,
    status: KeywordStatus,
    confirm: bool = False,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Enable, pause, or remove one keyword in an ad group.

    Find criterion_id with the search tool on keyword_view or
    ad_group_criterion. REMOVED cannot be undone. Dry run by default;
    confirm=true applies.
    """
    customer_id = utils.clean_customer_id(customer_id)
    client = _client(login_customer_id)

    rows = _query(
        client,
        customer_id,
        "SELECT ad_group.name, campaign.name, ad_group_criterion.criterion_id, "
        "ad_group_criterion.status, ad_group_criterion.keyword.text, "
        "ad_group_criterion.keyword.match_type "
        f"FROM ad_group_criterion WHERE ad_group.id = {int(ad_group_id)} "
        f"AND ad_group_criterion.criterion_id = {int(criterion_id)}",
    )
    if not rows:
        raise ToolError(f"Keyword {criterion_id} not found in ad group {ad_group_id}.")
    current = rows[0]

    svc = _service(client, "AdGroupCriterionService")
    rn = svc.ad_group_criterion_path(customer_id, ad_group_id, criterion_id)
    op = client.get_type("AdGroupCriterionOperation")
    if status == "REMOVED":
        op.remove = rn
    else:
        crit = op.update
        crit.resource_name = rn
        crit.status = getattr(client.enums.AdGroupCriterionStatusEnum, status)
        _set_mask(client, op, crit)

    req = client.get_type("MutateAdGroupCriteriaRequest")
    req.customer_id = customer_id
    req.operations.append(op)

    preview = {
        "action": "set_keyword_status",
        "keyword": f"{current.get('ad_group_criterion.keyword.text')} "
        f"[{current.get('ad_group_criterion.keyword.match_type')}] ({criterion_id})",
        "ad_group": current["ad_group.name"],
        "campaign": current["campaign.name"],
        "from": current.get("ad_group_criterion.status"),
        "to": status,
    }
    return _execute(
        svc, "mutate_ad_group_criteria", req, confirm, preview, "keyword status"
    )


@mutate_mcp.tool(annotations=_WRITE)
def set_keyword_bid(
    customer_id: str | int,
    ad_group_id: str | int,
    criterion_id: str | int,
    cpc_bid: float,
    confirm: bool = False,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Set a keyword's max CPC bid, in the account's currency units.

    Only meaningful on manual or enhanced CPC bidding. Dry run by default;
    the preview shows the current bid. confirm=true applies.
    """
    customer_id = utils.clean_customer_id(customer_id)
    if cpc_bid <= 0:
        raise ToolError("cpc_bid must be greater than zero.")
    client = _client(login_customer_id)

    rows = _query(
        client,
        customer_id,
        "SELECT ad_group.name, campaign.name, ad_group_criterion.criterion_id, "
        "ad_group_criterion.keyword.text, ad_group_criterion.keyword.match_type, "
        "ad_group_criterion.cpc_bid_micros, ad_group_criterion.effective_cpc_bid_micros "
        f"FROM ad_group_criterion WHERE ad_group.id = {int(ad_group_id)} "
        f"AND ad_group_criterion.criterion_id = {int(criterion_id)}",
    )
    if not rows:
        raise ToolError(f"Keyword {criterion_id} not found in ad group {ad_group_id}.")
    current = rows[0]

    svc = _service(client, "AdGroupCriterionService")
    op = client.get_type("AdGroupCriterionOperation")
    crit = op.update
    crit.resource_name = svc.ad_group_criterion_path(
        customer_id, ad_group_id, criterion_id
    )
    crit.cpc_bid_micros = _micros(cpc_bid)
    _set_mask(client, op, crit)

    req = client.get_type("MutateAdGroupCriteriaRequest")
    req.customer_id = customer_id
    req.operations.append(op)

    preview = {
        "action": "set_keyword_bid",
        "keyword": f"{current.get('ad_group_criterion.keyword.text')} "
        f"[{current.get('ad_group_criterion.keyword.match_type')}] ({criterion_id})",
        "ad_group": current["ad_group.name"],
        "campaign": current["campaign.name"],
        "from_cpc": _money(current.get("ad_group_criterion.cpc_bid_micros")),
        "from_effective_cpc": _money(
            current.get("ad_group_criterion.effective_cpc_bid_micros")
        ),
        "to_cpc": _money(crit.cpc_bid_micros),
    }
    return _execute(
        svc, "mutate_ad_group_criteria", req, confirm, preview, "keyword bid"
    )


# ----------------------------------------------------------------------------
# keyword match type (remove + recreate, since match type is immutable)
# ----------------------------------------------------------------------------


@mutate_mcp.tool(annotations=_WRITE)
def change_keyword_match_type(
    customer_id: str | int,
    ad_group_id: str | int,
    criterion_id: str | int,
    new_match_type: MatchType,
    confirm: bool = False,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Change a keyword's match type.

    Google Ads does not allow editing match type in place, so this removes the
    existing keyword and creates a new one with the same text, status and bid.
    The new keyword gets a new criterion ID and its history and quality score
    start over. Dry run by default; confirm=true applies.
    """
    customer_id = utils.clean_customer_id(customer_id)
    client = _client(login_customer_id)

    rows = _query(
        client,
        customer_id,
        "SELECT ad_group.name, campaign.name, ad_group_criterion.criterion_id, "
        "ad_group_criterion.status, ad_group_criterion.keyword.text, "
        "ad_group_criterion.keyword.match_type, ad_group_criterion.cpc_bid_micros, "
        "ad_group_criterion.final_urls "
        f"FROM ad_group_criterion WHERE ad_group.id = {int(ad_group_id)} "
        f"AND ad_group_criterion.criterion_id = {int(criterion_id)} "
        "AND ad_group_criterion.type = 'KEYWORD'",
    )
    if not rows:
        raise ToolError(f"Keyword {criterion_id} not found in ad group {ad_group_id}.")
    cur = rows[0]
    if cur.get("ad_group_criterion.keyword.match_type") == new_match_type:
        raise ToolError(f"Keyword is already {new_match_type}.")

    svc = _service(client, "AdGroupCriterionService")
    ad_group_rn = _service(client, "AdGroupService").ad_group_path(
        customer_id, ad_group_id
    )

    remove_op = client.get_type("AdGroupCriterionOperation")
    remove_op.remove = svc.ad_group_criterion_path(customer_id, ad_group_id, criterion_id)

    create_op = client.get_type("AdGroupCriterionOperation")
    crit = create_op.create
    crit.ad_group = ad_group_rn
    crit.status = getattr(
        client.enums.AdGroupCriterionStatusEnum,
        cur.get("ad_group_criterion.status", "ENABLED"),
    )
    crit.keyword.text = cur["ad_group_criterion.keyword.text"]
    crit.keyword.match_type = getattr(client.enums.KeywordMatchTypeEnum, new_match_type)
    if cur.get("ad_group_criterion.cpc_bid_micros"):
        crit.cpc_bid_micros = int(cur["ad_group_criterion.cpc_bid_micros"])
    for url in cur.get("ad_group_criterion.final_urls") or []:
        crit.final_urls.append(url)

    req = client.get_type("MutateAdGroupCriteriaRequest")
    req.customer_id = customer_id
    req.operations.extend([remove_op, create_op])

    preview = {
        "action": "change_keyword_match_type",
        "keyword": cur["ad_group_criterion.keyword.text"],
        "ad_group": cur["ad_group.name"],
        "campaign": cur["campaign.name"],
        "from": cur.get("ad_group_criterion.keyword.match_type"),
        "to": new_match_type,
        "keeps": {
            "status": cur.get("ad_group_criterion.status"),
            "cpc_bid": _money(cur.get("ad_group_criterion.cpc_bid_micros")),
        },
        "note": "Old keyword is removed; new one gets a new ID and fresh history.",
    }
    return _execute(
        svc, "mutate_ad_group_criteria", req, confirm, preview, "match type"
    )


# ----------------------------------------------------------------------------
# conversion goals (what bidding optimizes toward)
# ----------------------------------------------------------------------------

ConversionCategory = Literal[
    "PURCHASE", "SUBMIT_LEAD_FORM", "QUALIFIED_LEAD", "CONVERTED_LEAD",
    "BOOK_APPOINTMENT", "REQUEST_QUOTE", "GET_DIRECTIONS", "OUTBOUND_CLICK",
    "CONTACT", "SIGNUP", "PAGE_VIEW", "ADD_TO_CART", "BEGIN_CHECKOUT",
    "SUBSCRIBE_PAID", "PHONE_CALL_LEAD", "IMPORTED_LEAD", "ENGAGEMENT",
    "STORE_VISIT", "STORE_SALE", "DOWNLOAD",
]
ConversionOrigin = Literal[
    "WEBSITE", "GOOGLE_HOSTED", "APP", "CALL_FROM_ADS", "STORE", "YOUTUBE_HOSTED"
]


@mutate_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_conversion_goals(
    customer_id: str | int,
    campaign_id: str | int | None = None,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Show which conversion events bidding optimizes toward.

    Returns the account's conversion actions with their category and whether
    each is primary (used for bidding), and, if campaign_id is given, that
    campaign's bidding strategy and its per-category biddable flags.
    """
    customer_id = utils.clean_customer_id(customer_id)
    client = _client(login_customer_id)

    actions = _query(
        client,
        customer_id,
        "SELECT conversion_action.id, conversion_action.name, "
        "conversion_action.category, conversion_action.origin, "
        "conversion_action.status, conversion_action.primary_for_goal, "
        "conversion_action.type FROM conversion_action "
        "WHERE conversion_action.status != 'REMOVED'",
    )
    out: Dict[str, Any] = {"account_conversion_actions": actions}

    if campaign_id is not None:
        camp = _query(
            client,
            customer_id,
            "SELECT campaign.id, campaign.name, campaign.bidding_strategy_type, "
            "campaign.maximize_conversions.target_cpa_micros "
            f"FROM campaign WHERE campaign.id = {int(campaign_id)}",
        )
        if not camp:
            raise ToolError(f"Campaign {campaign_id} not found in {customer_id}.")
        goals = _query(
            client,
            customer_id,
            "SELECT campaign_conversion_goal.category, campaign_conversion_goal.origin, "
            "campaign_conversion_goal.biddable FROM campaign_conversion_goal "
            f"WHERE campaign.id = {int(campaign_id)}",
        )
        out["campaign"] = camp[0]
        out["campaign_conversion_goals"] = [
            g for g in goals if g.get("campaign_conversion_goal.biddable")
        ]
        out["campaign_conversion_goals_not_biddable_count"] = len(
            [g for g in goals if not g.get("campaign_conversion_goal.biddable")]
        )
    return out


@mutate_mcp.tool(annotations=_WRITE)
def set_campaign_conversion_goal(
    customer_id: str | int,
    campaign_id: str | int,
    category: ConversionCategory,
    biddable: bool,
    origin: ConversionOrigin = "WEBSITE",
    confirm: bool = False,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Turn a conversion category on or off for a campaign's bidding.

    Example: biddable=true for QUALIFIED_LEAD and biddable=false for
    SUBMIT_LEAD_FORM makes the campaign optimize toward validated leads only.
    Dry run by default; confirm=true applies. Check with get_conversion_goals
    first and after.
    """
    customer_id = utils.clean_customer_id(customer_id)
    client = _client(login_customer_id)

    camp = _query(
        client,
        customer_id,
        f"SELECT campaign.id, campaign.name FROM campaign "
        f"WHERE campaign.id = {int(campaign_id)}",
    )
    if not camp:
        raise ToolError(f"Campaign {campaign_id} not found in {customer_id}.")
    current = _query(
        client,
        customer_id,
        "SELECT campaign_conversion_goal.biddable FROM campaign_conversion_goal "
        f"WHERE campaign.id = {int(campaign_id)} "
        f"AND campaign_conversion_goal.category = '{category}' "
        f"AND campaign_conversion_goal.origin = '{origin}'",
    )

    svc = _service(client, "CampaignConversionGoalService")
    op = client.get_type("CampaignConversionGoalOperation")
    goal = op.update
    goal.resource_name = svc.campaign_conversion_goal_path(
        customer_id, campaign_id, category, origin
    )
    goal.biddable = biddable
    _set_mask(client, op, goal)

    req = client.get_type("MutateCampaignConversionGoalsRequest")
    req.customer_id = customer_id
    req.operations.append(op)

    preview = {
        "action": "set_campaign_conversion_goal",
        "campaign": f"{camp[0]['campaign.name']} ({campaign_id})",
        "category": category,
        "origin": origin,
        "from_biddable": current[0].get("campaign_conversion_goal.biddable") if current else "unknown",
        "to_biddable": biddable,
    }
    return _execute(
        svc, "mutate_campaign_conversion_goals", req, confirm, preview, "campaign goal"
    )


@mutate_mcp.tool(annotations=_WRITE)
def set_conversion_action_primary(
    customer_id: str | int,
    conversion_action_id: str | int,
    primary: bool,
    confirm: bool = False,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Make a conversion action primary (used for bidding) or secondary
    (observed only) at the account level. Dry run by default; confirm=true
    applies.
    """
    customer_id = utils.clean_customer_id(customer_id)
    client = _client(login_customer_id)

    rows = _query(
        client,
        customer_id,
        "SELECT conversion_action.id, conversion_action.name, "
        "conversion_action.category, conversion_action.primary_for_goal "
        f"FROM conversion_action WHERE conversion_action.id = {int(conversion_action_id)}",
    )
    if not rows:
        raise ToolError(f"Conversion action {conversion_action_id} not found.")
    cur = rows[0]

    svc = _service(client, "ConversionActionService")
    op = client.get_type("ConversionActionOperation")
    ca = op.update
    ca.resource_name = svc.conversion_action_path(customer_id, conversion_action_id)
    ca.primary_for_goal = primary
    _set_mask(client, op, ca)

    req = client.get_type("MutateConversionActionsRequest")
    req.customer_id = customer_id
    req.operations.append(op)

    preview = {
        "action": "set_conversion_action_primary",
        "conversion_action": f"{cur['conversion_action.name']} ({conversion_action_id})",
        "category": cur.get("conversion_action.category"),
        "from_primary": cur.get("conversion_action.primary_for_goal"),
        "to_primary": primary,
    }
    return _execute(
        svc, "mutate_conversion_actions", req, confirm, preview, "primary goal"
    )
