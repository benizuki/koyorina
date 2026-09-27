import base64

from backend.tests.test_api import INPUT, context, login  # noqa: F401


def test_sample_analysis_and_visualization_profile_round_trip(context):
    client, _ = context
    login(client)
    content = "記録日時,設備,生産数,不良内容\n2026-09-24 06:10,1号機,10,キズ\n".encode()
    analyzed = client.post("/api/sample-data/analyze", json={
        "name": "実績.csv", "content": base64.b64encode(content).decode(),
    })
    assert analyzed.status_code == 200, analyzed.text
    sample = analyzed.json()
    assert [column["suggested_role"] for column in sample["columns"]] == [
        "timestamp", "equipment", "quantity", "defect_category"]

    payload = {**INPUT, "creation_profile": {
        "app_pattern": "local_file_visualization",
        "work_category": "manufacturing", "app_type": "visualization",
        "goals": ["production_progress", "yield", "defect_pareto"],
        "other_goal": "", "production_day_start": "06:00", "sample_data": sample,
        "column_mappings": [{"column": column["name"], "role": column["suggested_role"]}
                            for column in sample["columns"]],
    }}
    created = client.post("/api/projects", json=payload)
    assert created.status_code == 201, created.text
    profile = created.json()["creation_profile"]
    assert profile["app_pattern"] == "local_file_visualization"
    assert profile["production_day_start"] == "06:00"
    assert profile["goals"][-1] == "defect_pareto"
    assert all(column["samples"] == [] for column in profile["sample_data"]["columns"])
    assert INPUT["name"] in created.json()["generation_prompt"]
    assert "AGENTS.md" not in created.json()["generation_prompt"]


def test_sample_analysis_requires_login(context):
    client, _ = context
    response = client.post("/api/sample-data/analyze", json={"name": "a.csv", "content": "YQ=="})
    assert response.status_code == 401


def test_generation_prompt_can_be_edited_and_saved(context):
    client, _ = context
    login(client)
    created = client.post("/api/projects", json=INPUT)
    assert created.status_code == 201, created.text
    project = created.json()
    edited = "売上の月別推移を1つのグラフで確認できるようにしてください。"

    updated = client.put(f"/api/projects/{project['id']}", json={
        **INPUT, "generation_prompt": edited, "revision": project["revision"],
    })

    assert updated.status_code == 200, updated.text
    assert updated.json()["generation_prompt"] == edited
    fetched = client.get("/api/projects").json()[0]
    assert fetched["generation_prompt"] == edited


PROMPT_PROJECT = {
    "name": "設備稼働ガント",
    "purpose": "",
    "fields": [], "tables": [],
    "generation_prompt": "設備ごとの稼働・停止をガントチャートで表示したい。\nTSVファイルを読み込む。",
    "creation_profile": {"app_pattern": "local_file_visualization", "mode": "prompt"},
}


def test_project_can_be_created_from_prompt_only(context):
    client, _ = context
    login(client)
    created = client.post("/api/projects", json=PROMPT_PROJECT)
    assert created.status_code == 201, created.text
    project = created.json()
    assert project["generation_prompt"] == PROMPT_PROJECT["generation_prompt"]
    # 一覧に出す目的は、依頼文の書き出しから作る。
    assert project["purpose"] == "設備ごとの稼働・停止をガントチャートで表示したい。"
    assert project["tables"] == [] and project["fields"] == []
    assert project["creation_profile"]["mode"] == "prompt"

    edited = "設備の稼働率を日ごとの棒グラフで表示したい。"
    updated = client.put(f"/api/projects/{project['id']}", json={
        **PROMPT_PROJECT, "generation_prompt": edited, "purpose": project["purpose"],
        "revision": project["revision"],
    })
    assert updated.status_code == 200, updated.text
    assert updated.json()["purpose"] == edited
    approved = client.post(f"/api/projects/{project['id']}/approve",
                           json={"revision": updated.json()["revision"]})
    assert approved.status_code == 200, approved.text


def test_prompt_mode_requires_a_prompt(context):
    client, _ = context
    login(client)
    response = client.post("/api/projects", json={**PROMPT_PROJECT, "generation_prompt": " "})
    assert response.status_code == 422


def test_guided_mode_still_requires_fields(context):
    client, _ = context
    login(client)
    response = client.post("/api/projects", json={**INPUT, "fields": [], "tables": []})
    assert response.status_code == 422
