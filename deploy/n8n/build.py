"""n8n workflow: every Monday at 9:00, fetch /digest.json and post it to Telegram.

Usage: RADAR_URL=http://192.168.1.214:8000 TELEGRAM_CHAT_ID=... TELEGRAM_CREDENTIAL=... python3 build.py [--test]
--test builds a manually triggered copy that posts immediately."""
import json
import os
import pathlib
import sys

test = "--test" in sys.argv
url = os.environ.get("RADAR_URL", "http://127.0.0.1:8000").rstrip("/")
credential = os.environ.get("TELEGRAM_CREDENTIAL", "telegram")


def node(name, type_, version, pos, params, **extra):
    return {"id": name.lower().replace(" ", "-"), "name": name, "type": type_, "typeVersion": version,
            "position": pos, "parameters": params, **extra}


trigger = (
    node("Manual run", "n8n-nodes-base.manualTrigger", 1, [0, 0], {})
    if test
    else node("Every Monday 9:00", "n8n-nodes-base.scheduleTrigger", 1.2, [0, 0],
              {"rule": {"interval": [{"field": "cronExpression", "expression": "0 9 * * 1"}]}})
)
nodes = [
    trigger,
    node("Weekly digest", "n8n-nodes-base.httpRequest", 4.2, [220, 0],
         {"url": f"{url}/digest.json?days=7", "options": {"timeout": 180000}},
         retryOnFail=True, maxTries=3, waitBetweenTries=30000),
    node("One item per message", "n8n-nodes-base.code", 2, [440, 0],
         {"jsCode": "return $json.messages.map(text => ({ json: { text } }));"}),
    node("Post to Telegram", "n8n-nodes-base.telegram", 1.2, [660, 0], {
        "chatId": os.environ.get("TELEGRAM_CHAT_ID", "SET_ME"), "text": "={{ $json.text }}",
        "additionalFields": {"appendAttribution": False, "parse_mode": "HTML", "disable_web_page_preview": True}},
        credentials={"telegramApi": {"id": credential, "name": "Telegram"}}),
]
names = [n["name"] for n in nodes]
wf = {
    "id": "communityRadarDigest" + ("Test" if test else ""),
    "name": "Community Radar · weekly digest" + (" (test)" if test else ""),
    "active": False, "nodes": nodes,
    "connections": {a: {"main": [[{"node": b, "type": "main", "index": 0}]]} for a, b in zip(names, names[1:])},
    "settings": {"executionOrder": "v1", "timezone": "Europe/Paris"},
}
out = pathlib.Path(__file__).with_name("workflow-test.json" if test else "workflow.json")
out.write_text(json.dumps(wf, ensure_ascii=False, indent=2))
print(out)
