from shizai_agent.core.browser_lifecycle import browser_cleanup_steps, normalize_browser_cleanup_plan


def test_auto_cleanup_only_closes_owned_new_tab():
    result = browser_cleanup_steps([
        {
            "id": "open",
            "component": "WebBrowser_CreateV3",
            "config": {"var_name": "web", "type": "chrome", "openNewTab": True},
        },
        {"id": "work", "component": "Basic_Delay_V1", "config": {}},
    ])

    assert result["status"] == "appended"
    assert result["steps"][0]["component"] == "WebBrowser_CloseWeb_V2"
    assert result["steps"][0]["config"]["web"] == {"variable": "web"}


def test_reused_user_page_is_never_auto_closed():
    result = browser_cleanup_steps([
        {
            "id": "open",
            "component": "WebBrowser_CreateV3",
            "config": {"var_name": "web", "type": "chrome", "openNewTab": False},
        },
    ])

    assert result["status"] == "skipped"
    assert result["steps"] == []


def test_explicit_keep_open_policy_wins_over_normalization():
    plan = {
        "browser_cleanup_policy": "keep_open",
        "steps": [{
            "id": "open",
            "component": "WebBrowser_CreateV3",
            "config": {"var_name": "web", "openNewTab": True},
        }],
    }

    result = normalize_browser_cleanup_plan(plan, available=True)

    assert result["status"] == "skipped"
    assert len(plan["steps"]) == 1
