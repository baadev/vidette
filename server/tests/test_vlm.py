"""The VLM client, the VLM judge and `vidette eval` comparing two judges side by side.

Every model here is `tests/fakes/openai_api.py`: an OpenAI-compatible server that answers
from a script. Nothing is downloaded, nothing leaves localhost, nothing is billed.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from pathlib import Path
from typing import Any

import httpx
import pytest
from fakes.openai_api import FakeVlmApi

from vidette.__main__ import main
from vidette.core.config import VidetteConfig, VlmBackend
from vidette.eval.dataset import load_dataset
from vidette.verdicts.base import Case
from vidette.verdicts.frames import spread
from vidette.verdicts.vlm import VlmJudge
from vidette.vlm.budget import FileLedger, MemoryLedger, Pace
from vidette.vlm.client import VlmClient, VlmError, parse_json_answer
from vidette.vlm.endpoint import VlmEndpoint, effective_backend, endpoint_for

REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLE = REPO_ROOT / "datasets" / "synthetic-sample"
KEY = "sk-test-0123456789abcdef"

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


def config(**vlm: Any) -> VidetteConfig:
    return VidetteConfig.model_validate({"understanding": {"vlm": vlm}})


def local_endpoint(**overrides: Any) -> VlmEndpoint:
    values: dict[str, Any] = {"base_url": "http://ollama.test/v1", "model": "fake-vlm"}
    values.update(overrides)
    return endpoint_for(config(local=values), VlmBackend.local)


def cloud_endpoint(**overrides: Any) -> VlmEndpoint:
    values: dict[str, Any] = {
        "preset": "openai",
        "base_url": "http://cloud.test/v1",
        "model": "fake-vlm",
        "api_key": KEY,
        "prices": {"usd_per_mtok_in": 0.4, "usd_per_mtok_out": 1.6},
    }
    values.update(overrides)
    return endpoint_for(config(cloud=values), VlmBackend.cloud)


def client_for(api: FakeVlmApi, endpoint: VlmEndpoint, ledger: Any = None) -> VlmClient:
    http = httpx.AsyncClient(transport=api.transport())
    return VlmClient(endpoint, ledger=ledger or MemoryLedger(), http=http)


SCHEMA = {"type": "object", "properties": {"x": {"type": "number"}}, "required": ["x"]}


async def ask(client: VlmClient, images: int = 2) -> Any:
    return await client.ask(
        system="sys", text="look", images=[b"\xff\xd8jpeg"] * images, schema=SCHEMA,
        schema_name="test",
    )  # fmt: skip


# --- endpoints and the person's choice ----------------------------------------------


def test_endpoints_say_where_frames_go() -> None:
    assert "stay on this machine" in endpoint_for(config(), VlmBackend.local).privacy
    remote_local = local_endpoint(base_url="http://192.0.2.20:11434/v1")
    assert "your own server at 192.0.2.20:11434" in remote_local.privacy
    gemini = endpoint_for(config(cloud={"preset": "gemini", "api_key": "k"}), VlmBackend.cloud)
    assert gemini.base_url.startswith("https://generativelanguage.googleapis.com")
    assert "sent to Google" in gemini.privacy
    assert KEY not in repr(cloud_endpoint())
    assert cloud_endpoint().token_param == "max_completion_tokens"


def test_stored_ui_choice_wins_unless_unavailable() -> None:
    plain = config(backend="local")
    assert effective_backend(plain, None) is VlmBackend.local
    assert effective_backend(plain, VlmBackend.none) is VlmBackend.none
    assert effective_backend(plain, VlmBackend.cloud) is VlmBackend.local  # no cloud section
    with_cloud = config(backend="local", cloud={"preset": "openai", "api_key": "k"})
    assert effective_backend(with_cloud, VlmBackend.cloud) is VlmBackend.cloud


# --- the client -------------------------------------------------------------------------


async def test_request_shape_auth_tokens_and_cost() -> None:
    api = FakeVlmApi(answer=lambda text, images, body: {"x": len(images)}, api_key=KEY)
    client = client_for(api, cloud_endpoint())
    answer = await ask(client, images=3)
    assert answer.data == {"x": 3}
    body = api.requests[0]["body"]
    assert api.requests[0]["authorization"] == f"Bearer {KEY}"
    assert body["model"] == "fake-vlm" and body["max_completion_tokens"] == 400
    assert body["response_format"]["type"] == "json_schema"
    assert answer.tokens_in == 1000 + 3 * 250 and answer.tokens_out == 40
    assert answer.usd == pytest.approx(1750 * 0.4 / 1e6 + 40 * 1.6 / 1e6)
    assert answer.calls == 1 and answer.latency_s >= 0


async def test_servers_without_structured_outputs_fall_back_and_it_sticks() -> None:
    api = FakeVlmApi(answer=lambda *_: {"x": 1}, reject_json_schema=True, fenced=True)
    client = client_for(api, local_endpoint())
    first = await ask(client)
    assert first.data == {"x": 1} and first.calls == 2
    second = await ask(client)
    assert second.calls == 1
    formats = [r["body"].get("response_format", {}).get("type") for r in api.requests]
    assert formats == ["json_schema", "json_object", "json_object"]


async def test_daily_budget_is_a_hard_stop_before_any_request() -> None:
    api = FakeVlmApi(answer=lambda *_: {"x": 1})
    ledger = MemoryLedger()
    client = client_for(api, local_endpoint(max_calls_per_day=2), ledger)
    await ask(client)
    await ask(client)
    with pytest.raises(VlmError) as excinfo:
        await ask(client)
    assert excinfo.value.kind == "budget" and len(api.requests) == 2
    assert (await ledger.today("local")).calls == 2


@pytest.mark.parametrize(
    ("api", "endpoint", "kind", "words"),
    [
        (FakeVlmApi(api_key="other"), "cloud", "auth", "rejected the API key"),
        (FakeVlmApi(model="something-else"), "local", "not_found", "ollama pull fake-vlm"),
        (FakeVlmApi(force_status=429), "cloud", "rate_limited", "429"),
        (FakeVlmApi(force_status=500, force_body="boom"), "cloud", "server", "boom"),
    ],
)
async def test_errors_are_typed_and_never_contain_the_key(
    api: FakeVlmApi, endpoint: str, kind: str, words: str
) -> None:
    chosen = cloud_endpoint() if endpoint == "cloud" else local_endpoint(timeout_s=0.2)
    with pytest.raises(VlmError) as excinfo:
        await ask(client_for(api, chosen))
    assert excinfo.value.kind == kind and words in str(excinfo.value)
    assert KEY not in str(excinfo.value)


async def test_a_slow_server_times_out_over_real_http() -> None:
    api = FakeVlmApi(answer=lambda *_: {"x": 1}, latency_s=1.0)
    with api.serve() as url:
        client = VlmClient(
            local_endpoint(base_url=url, timeout_s=0.3), ledger=MemoryLedger()
        )
        with pytest.raises(VlmError) as excinfo:
            await ask(client)
        await client.aclose()
    assert excinfo.value.kind == "timeout" and "within 0.3 s" in str(excinfo.value)


async def test_nothing_listening_points_at_ollama() -> None:
    client = VlmClient(local_endpoint(base_url="http://127.0.0.1:9/v1"), ledger=MemoryLedger())
    with pytest.raises(VlmError) as excinfo:
        await ask(client)
    await client.aclose()
    assert excinfo.value.kind == "network" and "ollama serve" in str(excinfo.value)


def test_json_answers_survive_fences_and_chatter() -> None:
    assert parse_json_answer('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_answer('Sure! {"a": 2} Hope that helps.') == {"a": 2}
    with pytest.raises(VlmError):
        parse_json_answer("I cannot see anything")


async def test_file_ledger_counts_per_backend_and_day(tmp_path: Path) -> None:
    ledger = FileLedger(tmp_path / "usage.json", clock=lambda: 1_794_700_000.0)
    await ledger.add("cloud", calls=2, usd=0.01)
    await ledger.add("cloud", calls=1, usd=0.0)
    await ledger.add("local", calls=5, usd=0.0)
    again = FileLedger(tmp_path / "usage.json", clock=lambda: 1_794_700_000.0)
    assert (await again.today("cloud")).calls == 3
    assert (await again.today("cloud")).usd == pytest.approx(0.01)
    assert (await again.today("local")).calls == 5


async def test_pace_spaces_calls_to_the_per_minute_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [0.0]
    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)
        now[0] += delay

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    pace = Pace(2, clock=lambda: now[0])
    await pace.wait()
    await pace.wait()
    now[0] += 10
    waited = await pace.wait()
    assert waited == pytest.approx(50.01) and slept == [pytest.approx(50.01)]


def test_frames_spread_keeps_first_and_last() -> None:
    items = [Path(f"{n}.jpg") for n in range(1, 11)]
    assert [p.stem for p in spread(items, 4)] == ["1", "4", "7", "10"]
    assert spread(items[:3], 4) == items[:3]


# --- the judge ----------------------------------------------------------------------


def durations_answer(table: dict[int, dict[str, Any]]) -> Any:
    """Answer by the clip length the judge reports ("lasted about 33 s"), which is unique
    per sample entry and independent of the machine's time zone."""

    def answer(text: str, images: list[bytes], body: dict[str, Any]) -> dict[str, Any]:
        match = re.search(r"lasted about (\d+) s", text)
        seconds = int(match.group(1)) if match else -1
        default = {"decision": "silent", "label": "nothing", "description": "-", "confidence": 0.6}
        return table.get(seconds, default)

    return answer


async def test_vlm_judge_on_a_sample_entry() -> None:
    dataset = load_dataset(SAMPLE)
    entry = next(e for e in dataset.labels.entries if e.id == "door-handle-night")
    api = FakeVlmApi(
        answer=durations_answer(
            {33: {"decision": "alert", "label": "door_handle", "description": "A person "
                  "pulls the front door handle.", "confidence": 0.91}}
        )
    )  # fmt: skip
    judge = VlmJudge(client=client_for(api, local_endpoint()), frames_per_call=4)
    verdict = await judge.judge(dataset.case(entry), dataset.policy_text("entry-interest"))
    assert verdict.alert and not verdict.abstained
    assert verdict.label == "door_handle" and verdict.confidence == 0.91
    assert verdict.rationale == "A person pulls the front door handle."
    assert verdict.images_sent == 3 and verdict.vlm_calls == 1  # the entry has 3 frames
    assert verdict.cost_usd == 0.0 and verdict.latency_s is not None
    text = api.requests[0]["body"]["messages"][1]["content"][0]["text"]
    assert "entry-interest" in text and "door_handle_attempt" not in text  # no ground truth
    assert judge.name == "vlm-local"


async def test_unsure_and_bad_answers_abstain() -> None:
    dataset = load_dataset(SAMPLE)
    case = dataset.case(dataset.labels.entries[0])
    policy = dataset.policy_text("entry-interest")
    unsure = FakeVlmApi(
        answer=lambda *_: {"decision": "unsure", "label": "dark", "description": "Too dark.",
                           "confidence": 0.2}
    )  # fmt: skip
    verdict = await VlmJudge(client=client_for(unsure, local_endpoint())).judge(case, policy)
    assert verdict.abstained and not verdict.alert and verdict.rationale == "unsure: Too dark."
    broken = FakeVlmApi(answer=lambda *_: {"nope": True}, fenced=False)
    broken.answer = lambda *_: {}  # valid JSON, no decision → unsure
    verdict = await VlmJudge(client=client_for(broken, local_endpoint())).judge(case, policy)
    assert verdict.abstained
    no_media = Case(incident=case.incident)
    verdict = await VlmJudge(client=client_for(unsure, local_endpoint())).judge(no_media, policy)
    assert verdict.abstained and verdict.rationale == "no frames to look at"


@needs_ffmpeg
async def test_frames_come_from_the_clip_when_there_are_none_on_disk(tmp_path: Path) -> None:
    clip = tmp_path / "clip.mp4"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi",
        "-i", "testsrc2=size=1920x1080:rate=10", "-t", "6", "-pix_fmt", "yuv420p", str(clip),
    )  # fmt: skip
    assert await proc.wait() == 0
    dataset = load_dataset(SAMPLE)
    case = Case(incident=dataset.case(dataset.labels.entries[0]).incident, media=(clip,))
    sent: list[bytes] = []

    def answer(text: str, images: list[bytes], body: dict[str, Any]) -> dict[str, Any]:
        sent.extend(images)
        return {"decision": "silent", "label": "test", "description": f"{len(images)} frames",
                "confidence": 1}  # fmt: skip

    api = FakeVlmApi(answer=answer)
    judge = VlmJudge(
        client=client_for(api, local_endpoint(max_image_side=512)),
        frames_per_call=4,
        cache_dir=tmp_path / "cache",
    )
    verdict = await judge.judge(case, dataset.policy_text("entry-interest"))
    assert verdict.rationale == "4 frames" and verdict.images_sent == 4
    assert len(list((tmp_path / "cache").rglob("*.jpg"))) == 4  # cached for the next run
    sent_file = tmp_path / "sent.jpg"
    sent_file.write_bytes(sent[0])
    for path, size in ((next((tmp_path / "cache").rglob("1.jpg")), "1280,720"),
                       (sent_file, "512,288")):  # fmt: skip
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0",
            str(path), stdout=asyncio.subprocess.PIPE,
        )  # fmt: skip
        out, _ = await proc.communicate()
        assert out.decode().strip() == size  # cached at <= 1280 px, sent at max_image_side


# --- vidette eval: two judges side by side --------------------------------------------


def test_eval_compares_local_and_cloud_side_by_side(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    local = FakeVlmApi(
        answer=durations_answer(
            {
                33: {"decision": "alert", "label": "door", "description": "-", "confidence": 0.9},
                41: {"decision": "alert", "label": "parcel", "description": "-", "confidence": 0.6},
            }
        )
    )
    cloud = FakeVlmApi(
        api_key=KEY,
        answer=durations_answer(
            {
                33: {"decision": "alert", "label": "door", "description": "-", "confidence": 0.95},
                12: {"decision": "unsure", "label": "dark", "description": "-", "confidence": 0.1},
            }
        ),
    )
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("CLOUD_KEY", KEY)
    with local.serve() as local_url, cloud.serve() as cloud_url:
        config_file = tmp_path / "vidette.yaml"
        config_file.write_text(
            "storage:\n"
            f"  database: {tmp_path / 'none.db'}\n"
            "understanding:\n"
            "  vlm:\n"
            "    local:\n"
            f"      base_url: {local_url}\n"
            "      model: fake-vlm\n"
            "    cloud:\n"
            "      preset: custom\n"
            f"      base_url: {cloud_url}\n"
            "      model: fake-vlm\n"
            "      api_key: ${CLOUD_KEY}\n"
            "      prices: {usd_per_mtok_in: 0.4, usd_per_mtok_out: 1.6}\n"
        )
        args = ["eval", str(SAMPLE), "--judge", "vlm-local", "--judge", "vlm-cloud",
                "--config", str(config_file)]  # fmt: skip
        assert main([*args, "--json"]) == 0
        report = json.loads(capsys.readouterr().out)
        assert main(args) == 0
        text = capsys.readouterr().out

    runs = {run["judge"]: run for run in report["runs"]}
    assert runs["vlm-local"]["precision"]["value"] == 0.5  # also alerted on the courier
    assert runs["vlm-cloud"]["precision"]["value"] == 1.0
    assert runs["vlm-local"]["recall"]["value"] == runs["vlm-cloud"]["recall"]["value"] == 1.0
    assert runs["vlm-local"]["false_alarms_per_day"] == 1.0
    assert runs["vlm-cloud"]["abstained"] == 1
    assert runs["vlm-local"]["measured"]["cost_usd"] == 0.0
    assert runs["vlm-cloud"]["measured"]["cost_usd"] > 0
    assert runs["vlm-cloud"]["measured"]["latency_s"]["p50"] is not None
    assert runs["vlm-cloud"]["vlm"]["calls"] == 5
    assert "Measured on this run" in text and "vlm-cloud" in text and KEY not in text
    usage = json.loads((tmp_path / "state" / "vidette" / "vlm-usage.json").read_text())
    (day,) = usage.values()
    assert day["local"]["calls"] == 10 and day["cloud"]["calls"] == 10  # two runs × 5


def test_eval_never_calls_a_vlm_unless_asked(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["eval", str(SAMPLE), "--json"]) == 0
    judges = {run["judge"] for run in json.loads(capsys.readouterr().out)["runs"]}
    assert judges == {"always", "never", "frigate-severity", "frigate-genai"}


def test_eval_without_a_cloud_section_says_what_to_add(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_file = tmp_path / "vidette.yaml"
    config_file.write_text("understanding:\n  vlm:\n    backend: local\n")
    assert main(["eval", str(SAMPLE), "--judge", "vlm-cloud", "--config", str(config_file)]) == 2
    assert "understanding.vlm.cloud" in capsys.readouterr().err
