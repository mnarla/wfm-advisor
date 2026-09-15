"""
nodes/agent.py — LangGraph tool-calling agent for Phase 2.

WHY THIS FILE EXISTS:
Phase 2 replaces the hardwired sequential pipeline (trend → vault → patch → synthesis)
with a Gemini-powered router. The router inspects the user's natural-language query,
selects the appropriate domain tool(s) from nodes/tools.py, calls them, and synthesises
a final natural-language response — all within a single LangGraph StateGraph loop.

Architecture (StateGraph with MessagesState):
  START → agent node → [tools_condition] → ToolNode → agent node → … → END

The agent terminates when the LLM produces a message with no further tool calls.
"""

import os
import logging
import warnings
from typing import Any, Dict

from dotenv import load_dotenv

# Suppress fixed sampling defaults warning from langchain_google_genai
warnings.filterwarnings("ignore", category=UserWarning, module="langchain_google_genai")
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import SystemMessage, HumanMessage
from langgraph.graph import StateGraph, START, MessagesState
from langgraph.prebuilt import ToolNode, tools_condition

from nodes.tools import ALL_MARKET_TOOLS

load_dotenv()
logger = logging.getLogger(__name__)

# Model used for the agent — gemini-3.5-flash-lite as requested
AGENT_MODEL = os.getenv("AGENT_MODEL", "gemini-3.5-flash-lite")

SYSTEM_PROMPT = """You are the Warframe Market Sell-Timing Advisor — an expert assistant
for Warframe Prime item trading. You help players make data-driven trading decisions
using real market data, vault schedules, and game patch analysis.

Scope: Warframe Market trading only. Never discuss platinum-to-real-money exchange rates,
non-Prime items, or topics unrelated to Warframe trading.

You have access to the following tools:
- get_price_trend: 90-day statistical price trend (slope, R², % change)
- get_vault_status: vault status, resurgence schedule, days since/until vault
- get_patch_impact: recent game balance buffs/nerfs from patch notes
- check_fair_price: evaluate if an offered price is fair, overpriced, or a bargain
- compare_set_vs_parts: compare selling as a full set vs. individual components
- get_market_signals: fetch all three signals (trend + vault + patch) in one call

Guidelines:
1. For sell/hold or buy queries: use get_market_signals, then reason about the combined signals.
2. For focused vault/trend/patch queries: use only the specific tool needed.
3. For fair-price checks: use check_fair_price with the exact offered price.
4. For set vs. parts: use compare_set_vs_parts.
5. For compare X vs Y: call get_market_signals for each item, then compare.
6. Always ground your reasoning in the data returned by the tools.
7. Be concise but complete. Lead with the action (SELL/HOLD/BUY/FAIR/etc.), then justify.
8. Use trading terminology players understand: plat, resurgence, vault cycle, etc.
"""


def _build_llm() -> Any:
    """
    Instantiates the ChatGoogleGenerativeAI LLM bound with all market tools.
    Falls back gracefully if GEMINI_API_KEY is not set.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or api_key == "your_gemini_api_key_here":
        raise RuntimeError(
            "GEMINI_API_KEY is not configured. Set it in your .env file or as an environment variable."
        )
    llm = ChatGoogleGenerativeAI(
        model=AGENT_MODEL,
        api_key=api_key,
    )
    return llm.bind_tools(ALL_MARKET_TOOLS)


def create_market_agent():
    """
    Builds and compiles the LangGraph StateGraph for the Phase 2 tool-calling agent.

    Graph structure:
        START → agent → (tools_condition) → tools → agent → … → END
    """
    llm = _build_llm()

    def call_model(state: MessagesState) -> Dict[str, Any]:
        """LLM decision node: reads the message history and produces a response (possibly with tool calls)."""
        messages = state["messages"]

        # Prepend system prompt if this is the first call
        if not any(isinstance(m, SystemMessage) for m in messages):
            messages = [SystemMessage(content=SYSTEM_PROMPT)] + list(messages)

        response = llm.invoke(messages)
        return {"messages": [response]}

    workflow = StateGraph(MessagesState)
    workflow.add_node("agent", call_model)
    workflow.add_node("tools", ToolNode(ALL_MARKET_TOOLS))

    workflow.add_edge(START, "agent")
    workflow.add_conditional_edges("agent", tools_condition)
    workflow.add_edge("tools", "agent")

    return workflow.compile()


def run_market_agent(query: str, db_path: str | None = None) -> Dict[str, Any]:
    """
    High-level entry point: sends a free-text query to the tool-calling agent
    and returns a structured result dict with the final response text.

    Args:
        query: Natural-language user query (e.g., "Is 45p fair for Rhino Prime Blueprint?").
        db_path: Optional path to override the SQLite DB. Defaults to the DB_PATH env var.

    Returns:
        {
            "status": "ok" | "error",
            "query": str,
            "response": str,       # Final agent response text
            "messages": [...],     # Full message history (for debugging)
        }
    """
    if db_path:
        os.environ["DB_PATH"] = db_path

    try:
        agent = create_market_agent()
        result = agent.invoke({"messages": [HumanMessage(content=query)]})

        messages = result.get("messages", [])
        # The last message is always the final agent response
        last_message = messages[-1] if messages else None

        # Extract plain text — handle both string content and structured content (list of dicts)
        if last_message is None:
            response_text = "No response generated."
        elif isinstance(last_message.content, str):
            response_text = last_message.content
        elif isinstance(last_message.content, list):
            # Gemini may return a list of content blocks
            response_text = " ".join(
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in last_message.content
            ).strip()
        else:
            response_text = str(last_message.content)

        return {
            "status": "ok",
            "query": query,
            "response": response_text,
            "messages": messages,
        }

    except Exception as e:
        logger.error(f"Market agent failed for query '{query}': {e}", exc_info=True)
        return {
            "status": "error",
            "query": query,
            "response": f"Agent error: {e}",
            "messages": [],
        }
