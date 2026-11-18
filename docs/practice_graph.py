from typing import TypedDict
from langgraph.graph import START, END, StateGraph

class DemoState(TypedDict):
    count: int

def add_one(state: DemoState) -> DemoState:
    return {"count": state["count"] + 1}

def choose_next(state: DemoState) -> str:
    if state["count"] >= 3:
        return END
    return "add_one"

builder = StateGraph(DemoState)
builder.add_node("add_one", add_one)
builder.add_edge(START, "add_one")
builder.add_conditional_edges(
    "add_one", choose_next,
    {END: END, "add_one": "add_one"},
)
graph = builder.compile()
result = graph.invoke({"count": 0})
print(result)
