from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver

from graph import build_graph

g = build_graph(InMemorySaver()).get_graph()
Path("docs").mkdir(exist_ok=True)

mermaid = g.draw_mermaid()
Path("docs/graph.mmd").write_text(mermaid, encoding="utf-8")
print(mermaid)

try:
    Path("docs/graph.png").write_bytes(g.draw_mermaid_png())
    print("Saved docs/graph.png")
except Exception as e:  # noqa: BLE001
    print(f"PNG skipped (needs internet): {e}")
