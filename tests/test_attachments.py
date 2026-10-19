"""Attachments: an image attached to a goal must reach the model.

Three things are worth pinning down, and they are the three that broke while
this was being built:

1. The bytes are written under `home/attachments/<task_id>/` and the event
   log carries a *reference*. A base64 blob in the log is re-read on every
   resume and copied into every state reconstruction, and the image is an
   artifact like any other.
2. The refusal cases are refusals. A pasted zip renamed `.png` is caught by
   magic bytes, not by the name the sender chose.
3. The vision gate fires *before* the task exists. A model that cannot see
   images is a settings mistake, and the cheapest place to say so is before a
   single event is written.
"""

from __future__ import annotations

import base64
import struct
import zlib

import pytest

pytest.importorskip("fastapi", reason="install with `pip install -e '.[api]'`")

from wukong.attachments import (  # noqa: E402
    MAX_BYTES,
    AttachmentError,
    ref_from_file,
    save_attachments,
)
from wukong.agent.state import replay  # noqa: E402
from wukong.observability.events import Event, EventType  # noqa: E402
from wukong.types import ImageRef, Message  # noqa: E402

from tests.conftest import ScriptedModel, ScriptedModels  # noqa: E402


def make_png(width: int = 4, height: int = 4) -> bytes:
    """A real PNG, small enough to paste into a test."""
    raw = b"".join(b"\x00" + bytes([9, 90, 160] * width) for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


class TestWireFormat:
    def test_a_user_message_with_images_becomes_parts(self, tmp_path) -> None:  # noqa: ANN001
        """OpenAI's multimodal shape, not a string with a URL in it."""
        # The ref reads its bytes from disk, so the file must exist. A fixed
        # `/tmp` path made this pass only where a previous run had left one
        # behind -- green locally, red on CI.
        image = tmp_path / "img.png"
        image.write_bytes(make_png())
        ref = ImageRef(path=str(image), media_type="image/png")
        body = Message(role="user", content="look at this", images=[ref]).to_provider_dict()
        content = body["content"]
        assert content[0] == {"type": "text", "text": "look at this"}
        assert content[1]["type"] == "image_url"
        assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")

    def test_a_message_without_images_is_unchanged(self) -> None:
        """The common case must not grow a wrapper."""
        body = Message(role="user", content="plain").to_provider_dict()
        assert body["content"] == "plain"

    def test_the_reference_never_carries_the_bytes(self) -> None:
        ref = ImageRef(path="/tmp/wk-img-test.png", sha256="a" * 64, bytes=71)
        dumped = ref.model_dump(mode="json")
        assert set(dumped) == {"path", "media_type", "name", "sha256", "bytes"}


class TestSaving:
    def test_the_file_lands_under_the_task(self, tmp_path) -> None:  # noqa: ANN001
        refs = save_attachments(
            tmp_path, "task_x", [{"media_type": "image/png", "data": base64.b64encode(make_png()).decode()}]
        )
        assert len(refs) == 1
        assert refs[0].sha256 and refs[0].bytes > 0
        written = list((tmp_path / "attachments" / "task_x").glob("*"))
        assert len(written) == 1
        assert written[0].read_bytes() == make_png()

    def test_the_same_image_twice_is_one_file(self, tmp_path) -> None:  # noqa: ANN001
        item = {"media_type": "image/png", "data": base64.b64encode(make_png()).decode()}
        save_attachments(tmp_path, "task_x", [item, dict(item)])
        assert len(list((tmp_path / "attachments" / "task_x").glob("*"))) == 1

    def test_a_data_url_prefix_is_accepted(self, tmp_path) -> None:  # noqa: ANN001
        blob = base64.b64encode(make_png()).decode()
        refs = save_attachments(
            tmp_path, "task_x", [{"media_type": "image/png", "data": f"data:image/png;base64,{blob}"}]
        )
        assert len(refs) == 1

    @pytest.mark.parametrize(
        ("payload", "fragment"),
        [
            ({"media_type": "image/png", "data": "!!!not-base64"}, "base64"),
            ({"media_type": "image/png", "data": ""}, "没有携带数据"),
            ({"media_type": "application/pdf", "data": "JVBERi0="}, "不支持"),
            # A zip renamed .png: the name says png, the bytes say otherwise.
            (
                {
                    "media_type": "image/png",
                    "data": base64.b64encode(b"PK\x03\x04" + b"\x00" * 32).decode(),
                },
                "内容不是",
            ),
        ],
    )
    def test_bad_input_is_refused_with_a_reason(self, tmp_path, payload, fragment) -> None:  # noqa: ANN001
        with pytest.raises(AttachmentError, match=fragment):
            save_attachments(tmp_path, "task_x", [payload])

    def test_too_many_images_are_refused(self, tmp_path) -> None:  # noqa: ANN001
        blob = base64.b64encode(make_png()).decode()
        items = [{"media_type": "image/png", "data": blob} for _ in range(9)]
        with pytest.raises(AttachmentError, match="最多附加"):
            save_attachments(tmp_path, "task_x", items)

    def test_too_big_is_refused(self, tmp_path) -> None:  # noqa: ANN001
        huge = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * (MAX_BYTES + 10)).decode()
        with pytest.raises(AttachmentError, match="超过"):
            save_attachments(
                tmp_path, "task_x", [{"media_type": "image/png", "data": huge}]
            )


class TestRefFromFile:
    def test_a_real_file_is_described_not_copied(self, tmp_path) -> None:  # noqa: ANN001
        path = tmp_path / "shot.png"
        path.write_bytes(make_png())
        ref = ref_from_file(path)
        assert ref.path == str(path.resolve())
        assert ref.media_type == "image/png"
        assert ref.name == "shot.png"

    def test_a_missing_file_is_refused(self, tmp_path) -> None:  # noqa: ANN001
        with pytest.raises(AttachmentError, match="不存在"):
            ref_from_file(tmp_path / "nope.png")

    def test_a_file_that_is_not_an_image_is_refused(self, tmp_path) -> None:  # noqa: ANN001
        path = tmp_path / "notes.txt"
        path.write_text("hello", encoding="utf-8")
        with pytest.raises(AttachmentError, match="不是受支持的图片类型"):
            ref_from_file(path)


class TestOverTheWire:
    def test_an_attached_image_reaches_the_model(self, api, settings) -> None:  # noqa: ANN001
        blob = base64.b64encode(make_png()).decode()
        client = api([])
        with client:
            created = client.post(
                "/api/v1/tasks",
                json={
                    "goal": "看看这张截图",
                    "images": [{"name": "shot.png", "media_type": "image/png", "data": blob}],
                },
            )
            assert created.status_code == 200, created.text
            task_id = created.json()["id"]
            client.post(f"/api/v1/tasks/{task_id}/wait", json={"timeout_s": 20})
            detail = client.get(f"/api/v1/tasks/{task_id}").json()

        assert detail["status"] == "completed"
        # The mock says out loud what it received; a silently dropped image
        # would look exactly like a working one otherwise.
        assert "Attached images received: 1" in (detail["answer"] or "")

    def test_the_event_log_carries_no_bytes(self, api, settings) -> None:  # noqa: ANN001
        blob = base64.b64encode(make_png()).decode()
        client = api([])
        with client:
            created = client.post(
                "/api/v1/tasks",
                json={
                    "goal": "x",
                    "images": [{"media_type": "image/png", "data": blob}],
                },
            )
            task_id = created.json()["id"]
            body = client.get(f"/api/v1/tasks/{task_id}/events").text

        assert "base64" not in body.lower()
        assert len(list((settings.home / "attachments" / task_id).glob("*"))) == 1

    def test_a_refused_image_is_a_400(self, api) -> None:  # noqa: ANN001
        client = api([])
        with client:
            response = client.post(
                "/api/v1/tasks",
                json={
                    "goal": "x",
                    "images": [{"media_type": "image/png", "data": "garbage!!"}],
                },
            )
        assert response.status_code == 400
        assert "base64" in response.text

    def test_the_attachment_route_serves_the_bytes(self, api, settings) -> None:  # noqa: ANN001
        blob = base64.b64encode(make_png()).decode()
        client = api([])
        with client:
            created = client.post(
                "/api/v1/tasks",
                json={
                    "goal": "x",
                    "images": [{"name": "shot.png", "media_type": "image/png", "data": blob}],
                },
            )
            task_id = created.json()["id"]
            meta = client.get(f"/api/v1/tasks/{task_id}").json()["images"]
            assert len(meta) == 1
            served = client.get(meta[0]["url"])

        assert served.status_code == 200
        assert served.content == make_png()
        assert served.headers["content-type"] == "image/png"

    def test_an_unknown_attachment_is_a_404(self, api) -> None:  # noqa: ANN001
        client = api([])
        with client:
            created = client.post("/api/v1/tasks", json={"goal": "x"}).json()
            response = client.get(f"/api/v1/tasks/{created['id']}/attachments/nope.png")
        assert response.status_code == 404

    def test_a_resume_still_has_the_image(self, api, settings) -> None:  # noqa: ANN001
        """State is rebuilt from events, so the attachment must be too."""
        blob = base64.b64encode(make_png()).decode()
        client = api([])
        with client:
            created = client.post(
                "/api/v1/tasks",
                json={
                    "goal": "x",
                    "images": [{"media_type": "image/png", "data": blob}],
                },
            )
            task_id = created.json()["id"]
            client.post(f"/api/v1/tasks/{task_id}/wait", json={"timeout_s": 20})
            # Rebuilt from the rows, inside the client's lifetime: a resume
            # has the log and nothing else, and that is the point.
            state = replay(api_events(client, task_id), task_id=task_id)

        assert len(state.images) == 1
        assert state.images[0].sha256
        assert state.images[0].path.endswith(".png")


def api_events(client, task_id: str) -> list[Event]:  # noqa: ANN001
    """The stored events, as `replay` wants them.

    Rebuilt from the API rows rather than read from the store, because that is
    what a *resuming process* has: the log and nothing else.
    """
    rows = client.get(f"/api/v1/tasks/{task_id}/events").json()
    return [
        Event(
            seq=int(row.get("seq", 0)),
            task_id=task_id,
            type=EventType(row["type"]),
            payload=row.get("payload") or {},
        )
        for row in rows
    ]


class TestTheVisionGate:
    def test_a_text_only_model_refuses_before_the_task_exists(
        self, settings, workspace
    ) -> None:  # noqa: ANN001
        """The cheapest place to catch it is before anything is written."""
        from wukong.agent.factory import build_agent
        from wukong.config import ModelSpec

        settings.models["textonly"] = ModelSpec(
            provider="mock",
            model="text",
            capabilities={"vision": False},
        )
        model = ScriptedModel(settings.models["textonly"], [{"content": "done"}])
        agent = None

        async def _run():  # noqa: ANN202
            nonlocal agent
            agent = await build_agent(settings=settings, models=ScriptedModels(model))
            session = agent.store.ensure_session(
                name="s", working_dir=str(workspace), model_alias="textonly"
            )
            ref = ImageRef(path=str(workspace / "app.py"), media_type="image/png")
            return await agent.runtime.run(
                "x", session_id=session, model_alias="textonly", images=[ref]
            )

        import asyncio

        with pytest.raises(ValueError, match="vision-capable"):
            asyncio.run(_run())
        assert agent is not None
        # No task was created -- the refusal cost nothing.
        assert agent.store.list_tasks(limit=10) == []
        agent.close()


class TestOverAgUi:
    """The console's path is AG-UI, not the REST route.

    Everything above goes through `POST /api/v1/tasks`, which the console
    does not use for new runs -- it posts to `/agui`. An attachment feature
    that only works on one of the two is a feature that works in tests and
    not in the product.
    """

    def test_an_attachment_sent_over_agui_reaches_the_model(self, api) -> None:  # noqa: ANN001
        blob = base64.b64encode(make_png()).decode()
        client = api([])
        with client:
            events = parse_sse(
                client.post(
                    "/agui",
                    json={
                        "messages": [{"role": "user", "content": "看看这张图"}],
                        "attachments": [
                            {"name": "shot.png", "media_type": "image/png", "data": blob}
                        ],
                    },
                ).text
            )

        answers = [
            e for e in events if e.get("type") == "TEXT_MESSAGE_CONTENT"
        ]
        joined = "".join(e.get("delta", "") for e in answers)
        assert "Attached images received: 1" in joined

    def test_a_refused_attachment_ends_the_run_as_an_error(self, api) -> None:  # noqa: ANN001
        client = api([])
        with client:
            events = parse_sse(
                client.post(
                    "/agui",
                    json={
                        "messages": [{"role": "user", "content": "x"}],
                        "attachments": [{"media_type": "image/png", "data": "nope!!"}],
                    },
                ).text
            )

        # RUN_ERROR is terminal here, exactly as it is for an empty input:
        # the stream never started, so there is no run to finish.
        assert types_of(events)[-1] == "RUN_ERROR"
        assert "base64" in (events[-1].get("message") or "")
        assert events[-1].get("code") == "bad_attachment"


def parse_sse(text: str) -> list[dict]:
    out: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:") and line[5:].strip():
            import json

            out.append(json.loads(line[5:].strip()))
    return out


def types_of(events: list[dict]) -> list[str]:
    return [e["type"] for e in events]
