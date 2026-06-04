"""
LangGraph Orchestrator
======================
Wires all agents into a directed graph:

  data_collector
       │
       ├─ (no matches) ──→ report_agent → END
       │
       ▼
  model_predictor
       │
       ▼
  odds_analyzer
       │
       ├─ (no signals) ──→ report_agent → END
       │
       ▼
  risk_manager
       │
       ▼
  execution_agent
       │
       ▼
  report_agent
       │
       ▼
      END

On any `state.error`, the graph routes to END immediately.
"""
from __future__ import annotations

from typing import Literal

from langgraph.graph import END, StateGraph

from sporting_edge.agents.data_collector import data_collector_node
from sporting_edge.agents.execution_agent import execution_agent_node
from sporting_edge.agents.model_predictor import model_predictor_node
from sporting_edge.agents.odds_analyzer import odds_analyzer_node
from sporting_edge.agents.report_agent import report_agent_node
from sporting_edge.agents.risk_manager import risk_manager_node
from sporting_edge.config import settings
from sporting_edge.config.logging import get_logger
from sporting_edge.models.schemas import AgentState

log = get_logger(__name__)

# ── State adapter (LangGraph requires dict state) ────────────────────────────

def _state_to_dict(state: AgentState) -> dict:
    return state.model_dump()


def _dict_to_state(d: dict) -> AgentState:
    return AgentState(**d)


# ── Node wrappers (convert dict ↔ AgentState) ────────────────────────────────

def _wrap(node_fn):
    """Convert a node that takes/returns AgentState to dict-based for LangGraph."""
    async def wrapped(state: dict) -> dict:
        agent_state = AgentState(**state)
        result = await node_fn(agent_state)
        return result.model_dump()
    wrapped.__name__ = node_fn.__name__
    return wrapped


# ── Conditional edges ─────────────────────────────────────────────────────────

def _after_data_collector(state: dict) -> Literal["model_predictor", "report_agent", "__end__"]:
    if state.get("error"):
        return "__end__"
    if not state.get("matches"):
        log.info("no_matches_found_skip_to_report")
        return "report_agent"
    return "model_predictor"


def _after_odds_analyzer(state: dict) -> Literal["risk_manager", "report_agent"]:
    if state.get("error"):
        return "report_agent"
    if not state.get("signals"):
        log.info("no_signals_found_skip_to_report")
        return "report_agent"
    return "risk_manager"


def _after_risk_manager(state: dict) -> Literal["execution_agent", "report_agent"]:
    approved = [d for d in state.get("decisions", []) if d.get("approved")]
    if not approved:
        log.info("no_approved_bets_skip_to_report")
        return "report_agent"
    return "execution_agent"


# ── Graph construction ────────────────────────────────────────────────────────

def build_graph() -> StateGraph:
    """
    Build and compile the LangGraph StateGraph.
    Returns a compiled graph ready to be `.ainvoke()`'d.
    """
    graph = StateGraph(dict)   # state is a plain dict (AgentState serialized)

    # Register nodes
    graph.add_node("data_collector", _wrap(data_collector_node))
    graph.add_node("model_predictor", _wrap(model_predictor_node))
    graph.add_node("odds_analyzer", _wrap(odds_analyzer_node))
    graph.add_node("risk_manager", _wrap(risk_manager_node))
    graph.add_node("execution_agent", _wrap(execution_agent_node))
    graph.add_node("report_agent", _wrap(report_agent_node))

    # Entry point
    graph.set_entry_point("data_collector")

    # Edges
    graph.add_conditional_edges(
        "data_collector",
        _after_data_collector,
        {
            "model_predictor": "model_predictor",
            "report_agent": "report_agent",
            "__end__": END,
        },
    )
    graph.add_edge("model_predictor", "odds_analyzer")
    graph.add_conditional_edges(
        "odds_analyzer",
        _after_odds_analyzer,
        {
            "risk_manager": "risk_manager",
            "report_agent": "report_agent",
        },
    )
    graph.add_conditional_edges(
        "risk_manager",
        _after_risk_manager,
        {
            "execution_agent": "execution_agent",
            "report_agent": "report_agent",
        },
    )
    graph.add_edge("execution_agent", "report_agent")
    graph.add_edge("report_agent", END)

    return graph.compile()


# ── Public runner ─────────────────────────────────────────────────────────────

_compiled_graph = None


def _get_graph():
    global _compiled_graph
    if _compiled_graph is None:
        _compiled_graph = build_graph()
    return _compiled_graph


async def run_pipeline(
    league_ids: list[int] | None = None,
    triggered_by: str = "scheduler",
) -> AgentState:
    """
    Execute the full pipeline once.
    Returns the final AgentState.
    """
    initial_state = AgentState(
        target_league_ids=league_ids or settings.active_league_ids,
        triggered_by=triggered_by,
    )

    log.info(
        "pipeline_start",
        run_id=initial_state.run_id,
        leagues=initial_state.target_league_ids,
        triggered_by=triggered_by,
    )

    graph = _get_graph()
    final_dict = await graph.ainvoke(initial_state.model_dump())
    final_state = AgentState(**final_dict)

    log.info(
        "pipeline_complete",
        run_id=final_state.run_id,
        completed_nodes=final_state.completed_nodes,
        matches=len(final_state.matches),
        signals=len(final_state.signals),
        bets=len(final_state.bets_placed),
        error=final_state.error,
    )

    return final_state
