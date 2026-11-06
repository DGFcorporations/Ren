"""Streamlit log view. Process ownership lives in the launcher, never in UI reruns."""

from __future__ import annotations

import os

import streamlit as st

from workbench_pilot.monitor_bridge import BridgeClient

st.set_page_config(
    page_title="Ren • Output monitor",
    page_icon="◉",
    layout="wide",
    menu_items={
        "Get help": None,
        "Report a bug": None,
        "About": "Local G1 stdout/stderr monitor. No live voice/video.",
    },
)


# Static CSS only: captured output is never interpreted as HTML.
st.html("""<style>
[data-testid="stTextArea"] textarea:disabled {
  color: #e5e7eb !important; -webkit-text-fill-color: #e5e7eb !important;
  background: #111827 !important; opacity: 1 !important;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace !important;
  font-size: 12px !important; line-height: 1.5 !important;
}
</style>""")


@st.cache_resource
def bridge_client() -> BridgeClient:
    return BridgeClient(
        os.environ.get("REN_MONITOR_CONTROL_URL", ""),
        os.environ.get("REN_MONITOR_CONTROL_TOKEN", ""),
    )


st.title("Ren · Output monitor")
st.caption("Local process output · G1 offline · No microphone, camera, WebRTC or provider calls")
st.info(
    "This view captures only the Ren process started by this launcher. "
    "It cannot attach to another process, send prompts, approve Ren actions, or enable live services."
)
try:
    client = bridge_client()
except (ValueError, RuntimeError):
    st.error("Start this app with ren-monitor. The launcher supplies a private local control bridge.")
    st.stop()


@st.fragment(run_every=0.5)
def output_panel():
    try:
        snapshot = client.snapshot()
    except RuntimeError:
        st.error(
            "The local control bridge is unavailable. No replacement process will be started automatically."
        )
        return

    state = snapshot["state"]
    a, b, c, d = st.columns(4)
    a.metric("Process state", state)
    b.metric("Owned PID", str(snapshot["pid"]) if snapshot["pid"] is not None else "—")
    c.metric("Exit code", str(snapshot["returncode"]) if snapshot["returncode"] is not None else "—")
    d.metric("Dropped old lines", snapshot["dropped_lines"])

    left, middle, right, spacer = st.columns([1, 1, 1, 5])
    action = None
    if left.button("Start Ren", disabled=state not in {"idle", "exited", "failed"}, width="stretch"):
        action = "start"
    if middle.button("Stop Ren", disabled=state not in {"starting", "running"}, width="stretch"):
        action = "stop"
    if right.button("Clear view", width="stretch"):
        action = "clear"
    if action:
        try:
            client.command(action)
            st.rerun(scope="fragment")
        except RuntimeError:
            st.error("The process command was not confirmed. Check the status; do not assume it succeeded.")

    st.caption(
        f"Refresh: about 0.5 seconds · Generation {snapshot['generation']} · "
        f"{snapshot['total_lines']} captured lines · stdout and stderr combined. "
        "Running means the process is alive, not that every Ren feature is ready."
    )
    if snapshot["truncated"]:
        st.warning(
            "Output is bounded. Some long or older output has been truncated; "
            "this is a live tail, not an archive."
        )
    if snapshot["error"]:
        st.error(snapshot["error"])
    # Set session state before creating the widget on each fragment rerun. A
    # constant widget key alone with a changing default value can display stale logs.
    st.session_state["ren_captured_output"] = snapshot["text"]
    st.text_area("Ren stdout + stderr", key="ren_captured_output", height=460, disabled=True)
    st.caption(
        "Clear view does not clear Ren's data. Stop affects only the owned child process. "
        "Obvious secret fields are redacted on a best-effort basis; never log credentials."
    )
    st.link_button("Open the local offline Ren interface", snapshot["ren_origin"])


output_panel()
st.divider()
st.write(
    "**Still unverified:** live speech recognition/synthesis, semantic-memory services, "
    "real-account browser control, phone control, SMS, and bidirectional audio/video."
)
