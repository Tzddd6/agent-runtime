"""
sierra-research/tau2-bench (~2.5k stars) Airline Domain Runner on AIOS Formal Kernel + DeepSeek.

Executes official tau2-bench `data/tau2/domains/airline/tasks.json` multi-turn Agent-User-Tool
interactions using the exact 14 AirlineTools over isolated per-task copies of `db.json`.
Every Agent LLM turn, User Simulator turn, and Tool call is routed through `AIOSKernelRuntime`
(`LLMQuery` -> `ModelService` -> DeepSeek; `ToolQuery` -> `AutoTool`), and graded via
official tau2-bench DB state hash matching + `communicate_info` verification (`pass^1`).

API keys are read strictly from `DEEPSEEK_API_KEY` in process memory and never written to disk.
"""
from concurrent.futures import ThreadPoolExecutor
import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from aios.llm_core.model_service import ModelService
from cerebrum.interface import AutoTool
from cerebrum.llm.apis import LLMQuery
from cerebrum.tool.apis import ToolQuery
from runtime.launch import AIOSKernelRuntime

AIRPORTS = [
    {"iata": "SFO", "city": "San Francisco"}, {"iata": "JFK", "city": "New York"},
    {"iata": "LAX", "city": "Los Angeles"}, {"iata": "ORD", "city": "Chicago"},
    {"iata": "DFW", "city": "Dallas"}, {"iata": "DEN", "city": "Denver"},
    {"iata": "SEA", "city": "Seattle"}, {"iata": "ATL", "city": "Atlanta"},
    {"iata": "MIA", "city": "Miami"}, {"iata": "BOS", "city": "Boston"},
    {"iata": "PHX", "city": "Phoenix"}, {"iata": "IAH", "city": "Houston"},
    {"iata": "LAS", "city": "Las Vegas"}, {"iata": "MCO", "city": "Orlando"},
    {"iata": "EWR", "city": "Newark"}, {"iata": "CLT", "city": "Charlotte"},
    {"iata": "MSP", "city": "Minneapolis"}, {"iata": "DTW", "city": "Detroit"},
    {"iata": "PHL", "city": "Philadelphia"}, {"iata": "LGA", "city": "LaGuardia"},
]

TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {"type": "function", "function": {"name": "get_user_details", "description": "Get details of a user including reservations and payment methods.", "parameters": {"type": "object", "properties": {"user_id": {"type": "string"}}, "required": ["user_id"]}}},
    {"type": "function", "function": {"name": "get_reservation_details", "description": "Get details of a reservation.", "parameters": {"type": "object", "properties": {"reservation_id": {"type": "string"}}, "required": ["reservation_id"]}}},
    {"type": "function", "function": {"name": "get_flight_status", "description": "Get status of a flight on a date (YYYY-MM-DD).", "parameters": {"type": "object", "properties": {"flight_number": {"type": "string"}, "date": {"type": "string"}}, "required": ["flight_number", "date"]}}},
    {"type": "function", "function": {"name": "list_all_airports", "description": "List all airports and their IATA codes.", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "search_direct_flight", "description": "Search direct flights between origin and destination on date (YYYY-MM-DD).", "parameters": {"type": "object", "properties": {"origin": {"type": "string"}, "destination": {"type": "string"}, "date": {"type": "string"}}, "required": ["origin", "destination", "date"]}}},
    {"type": "function", "function": {"name": "search_onestop_flight", "description": "Search one-stop flights between origin and destination on date (YYYY-MM-DD).", "parameters": {"type": "object", "properties": {"origin": {"type": "string"}, "destination": {"type": "string"}, "date": {"type": "string"}}, "required": ["origin", "destination", "date"]}}},
    {"type": "function", "function": {"name": "calculate", "description": "Calculate a mathematical expression (numbers, +-*/()).", "parameters": {"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]}}},
    {"type": "function", "function": {"name": "cancel_reservation", "description": "Cancel the whole reservation.", "parameters": {"type": "object", "properties": {"reservation_id": {"type": "string"}}, "required": ["reservation_id"]}}},
    {"type": "function", "function": {"name": "send_certificate", "description": "Send a travel certificate to a user.", "parameters": {"type": "object", "properties": {"user_id": {"type": "string"}, "amount": {"type": "integer"}}, "required": ["user_id", "amount"]}}},
    {"type": "function", "function": {"name": "transfer_to_human_agents", "description": "Transfer the user to a human agent with a summary.", "parameters": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}}},
    {"type": "function", "function": {"name": "update_reservation_baggages", "description": "Update baggages of a reservation.", "parameters": {"type": "object", "properties": {"reservation_id": {"type": "string"}, "total_baggages": {"type": "integer"}, "nonfree_baggages": {"type": "integer"}, "payment_id": {"type": "string"}}, "required": ["reservation_id", "total_baggages", "nonfree_baggages", "payment_id"]}}},
    {"type": "function", "function": {"name": "update_reservation_flights", "description": "Update flights and cabin of a reservation. Include ALL flight segments in the new reservation.", "parameters": {"type": "object", "properties": {"reservation_id": {"type": "string"}, "cabin": {"type": "string", "enum": ["basic_economy", "economy", "business"]}, "flights": {"type": "array", "items": {"type": "object", "properties": {"flight_number": {"type": "string"}, "date": {"type": "string"}}, "required": ["flight_number", "date"]}}, "payment_id": {"type": "string"}}, "required": ["reservation_id", "cabin", "flights", "payment_id"]}}},
    {"type": "function", "function": {"name": "update_reservation_passengers", "description": "Update passenger information of a reservation.", "parameters": {"type": "object", "properties": {"reservation_id": {"type": "string"}, "passengers": {"type": "array", "items": {"type": "object", "properties": {"first_name": {"type": "string"}, "last_name": {"type": "string"}, "dob": {"type": "string"}}, "required": ["first_name", "last_name", "dob"]}}}, "required": ["reservation_id", "passengers"]}}},
    {"type": "function", "function": {"name": "book_reservation", "description": "Book a new flight reservation.", "parameters": {"type": "object", "properties": {"user_id": {"type": "string"}, "origin": {"type": "string"}, "destination": {"type": "string"}, "flight_type": {"type": "string", "enum": ["one_way", "round_trip"]}, "cabin": {"type": "string", "enum": ["basic_economy", "economy", "business"]}, "flights": {"type": "array", "items": {"type": "object", "properties": {"flight_number": {"type": "string"}, "date": {"type": "string"}}, "required": ["flight_number", "date"]}}, "passengers": {"type": "array", "items": {"type": "object", "properties": {"first_name": {"type": "string"}, "last_name": {"type": "string"}, "dob": {"type": "string"}}, "required": ["first_name", "last_name", "dob"]}}, "payment_methods": {"type": "array", "items": {"type": "object", "properties": {"payment_id": {"type": "string"}, "amount": {"type": "number"}}, "required": ["payment_id", "amount"]}}, "total_baggages": {"type": "integer"}, "nonfree_baggages": {"type": "integer"}, "insurance": {"type": "string", "enum": ["yes", "no"]}}, "required": ["user_id", "origin", "destination", "flight_type", "cabin", "flights", "passengers", "payment_methods", "total_baggages", "nonfree_baggages", "insurance"]}}},
]


class AirlineToolKit:
    """Exact pure-Python mirror of sierra-research/tau2-bench AirlineTools."""

    def __init__(self, db: Dict[str, Any]):
        self.db = db
        self.transferred = False

    def db_hash(self) -> str:
        raw = json.dumps(self.db, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _get_user(self, user_id: str) -> Dict[str, Any]:
        if user_id not in self.db["users"]:
            raise ValueError(f"User {user_id} not found")
        return self.db["users"][user_id]

    def _get_reservation(self, reservation_id: str) -> Dict[str, Any]:
        if reservation_id not in self.db["reservations"]:
            raise ValueError(f"Reservation {reservation_id} not found")
        return self.db["reservations"][reservation_id]

    def _get_flight(self, flight_number: str) -> Dict[str, Any]:
        if flight_number not in self.db["flights"]:
            raise ValueError(f"Flight {flight_number} not found")
        return self.db["flights"][flight_number]

    def _get_flight_instance(self, flight_number: str, date: str) -> Dict[str, Any]:
        fl = self._get_flight(flight_number)
        if date not in fl["dates"]:
            raise ValueError(f"Flight {flight_number} not found on date {date}")
        return fl["dates"][date]

    def _payment_for_update(self, user: Dict[str, Any], payment_id: str, total_price: int) -> Optional[Dict[str, Any]]:
        if payment_id not in user["payment_methods"]:
            raise ValueError("Payment method not found")
        pm = user["payment_methods"][payment_id]
        if pm["source"] == "certificate":
            raise ValueError("Certificate cannot be used to update reservation")
        if pm["source"] == "gift_card" and pm["amount"] < total_price:
            raise ValueError("Gift card balance is not enough")
        if pm["source"] == "gift_card":
            pm["amount"] -= total_price
        if total_price != 0:
            return {"payment_id": payment_id, "amount": total_price}
        return None

    def _search_direct(self, date: str, origin: Optional[str] = None, destination: Optional[str] = None, leave_after: Optional[str] = None) -> List[Dict[str, Any]]:
        out = []
        for fl in self.db["flights"].values():
            if (
                (origin is None or fl["origin"] == origin)
                and (destination is None or fl["destination"] == destination)
                and (date in fl["dates"])
                and (fl["dates"][date]["status"] == "available")
                and (leave_after is None or fl["scheduled_departure_time_est"] >= leave_after)
            ):
                d = fl["dates"][date]
                out.append({
                    "flight_number": fl["flight_number"],
                    "origin": fl["origin"],
                    "destination": fl["destination"],
                    "status": "available",
                    "scheduled_departure_time_est": fl["scheduled_departure_time_est"],
                    "scheduled_arrival_time_est": fl["scheduled_arrival_time_est"],
                    "available_seats": d["available_seats"],
                    "prices": d["prices"],
                })
        return out

    def dispatch(self, name: str, args: Dict[str, Any]) -> Any:
        fn = getattr(self, f"tool_{name}", None)
        if fn is None:
            raise ValueError(f"Unknown tool: {name}")
        return fn(**(args or {}))

    def tool_get_user_details(self, user_id: str) -> Any:
        return deepcopy(self._get_user(user_id))

    def tool_get_reservation_details(self, reservation_id: str) -> Any:
        return deepcopy(self._get_reservation(reservation_id))

    def tool_get_flight_status(self, flight_number: str, date: str) -> str:
        return self._get_flight_instance(flight_number, date)["status"]

    def tool_list_all_airports(self) -> Any:
        return AIRPORTS

    def tool_search_direct_flight(self, origin: str, destination: str, date: str) -> Any:
        return self._search_direct(date=date, origin=origin, destination=destination)

    def tool_search_onestop_flight(self, origin: str, destination: str, date: str) -> Any:
        results = []
        for r1 in self._search_direct(date=date, origin=origin, destination=None):
            r1["date"] = date
            date2 = f"2024-05-{int(date[-2:]) + 1:02d}" if "+1" in r1["scheduled_arrival_time_est"] else date
            for r2 in self._search_direct(date=date2, origin=r1["destination"], destination=destination, leave_after=r1["scheduled_arrival_time_est"]):
                r2["date"] = date2
                results.append([deepcopy(r1), deepcopy(r2)])
        return results

    def tool_calculate(self, expression: str) -> str:
        if not all(c in "0123456789+-*/(). " for c in expression):
            raise ValueError("Invalid characters in expression")
        return str(round(float(eval(expression, {"__builtins__": None}, {})), 2))

    def tool_cancel_reservation(self, reservation_id: str) -> Any:
        res = self._get_reservation(reservation_id)
        refunds = [{"payment_id": p["payment_id"], "amount": -p["amount"]} for p in res["payment_history"]]
        res["payment_history"].extend(refunds)
        res["status"] = "cancelled"
        return deepcopy(res)

    def tool_send_certificate(self, user_id: str, amount: int) -> str:
        user = self._get_user(user_id)
        for pid in ("certificate_3221322", "certificate_3221323", "certificate_3221324"):
            if pid not in user["payment_methods"]:
                user["payment_methods"][pid] = {"id": pid, "amount": amount, "source": "certificate"}
                return f"Certificate {pid} added to user {user_id} with amount {amount}."
        raise ValueError("Too many certificates")

    def tool_transfer_to_human_agents(self, summary: str) -> str:
        self.transferred = True
        return "Transfer successful"

    def tool_update_reservation_baggages(self, reservation_id: str, total_baggages: int, nonfree_baggages: int, payment_id: str) -> Any:
        res = self._get_reservation(reservation_id)
        user = self._get_user(res["user_id"])
        total_price = 50 * max(0, nonfree_baggages - res["nonfree_baggages"])
        payment = self._payment_for_update(user, payment_id, total_price)
        if payment is not None:
            res["payment_history"].append(payment)
        res["total_baggages"] = total_baggages
        res["nonfree_baggages"] = nonfree_baggages
        return deepcopy(res)

    def tool_update_reservation_flights(self, reservation_id: str, cabin: str, flights: List[Dict[str, Any]], payment_id: str) -> Any:
        res = self._get_reservation(reservation_id)
        user = self._get_user(res["user_id"])
        total_price = 0
        new_flights = []
        for fi in flights:
            fn = fi["flight_number"]
            dt = fi["date"]
            fl = self._get_flight(fn)
            existing = next((rf for rf in res["flights"] if rf["flight_number"] == fn and rf["date"] == dt and cabin == res["cabin"]), None)
            if existing:
                total_price += existing["price"] * len(res["passengers"])
                new_flights.append(existing)
                continue
            inst = self._get_flight_instance(fn, dt)
            if inst["status"] != "available":
                raise ValueError(f"Flight {fn} not available on date {dt}")
            if inst["available_seats"][cabin] < len(res["passengers"]):
                raise ValueError(f"Not enough seats on flight {fn}")
            price = inst["prices"][cabin]
            new_flights.append({"origin": fl["origin"], "destination": fl["destination"], "flight_number": fn, "date": dt, "price": price})
            total_price += price * len(res["passengers"])
        total_price -= sum(rf["price"] for rf in res["flights"]) * len(res["passengers"])
        payment = self._payment_for_update(user, payment_id, total_price)
        if payment is not None:
            res["payment_history"].append(payment)
        res["flights"] = new_flights
        res["cabin"] = cabin
        return deepcopy(res)

    def tool_update_reservation_passengers(self, reservation_id: str, passengers: List[Dict[str, Any]]) -> Any:
        res = self._get_reservation(reservation_id)
        if len(passengers) != len(res["passengers"]):
            raise ValueError("Number of passengers does not match")
        res["passengers"] = deepcopy(passengers)
        return deepcopy(res)

    def tool_book_reservation(
        self, user_id: str, origin: str, destination: str, flight_type: str, cabin: str,
        flights: List[Dict[str, Any]], passengers: List[Dict[str, Any]], payment_methods: List[Dict[str, Any]],
        total_baggages: int, nonfree_baggages: int, insurance: str,
    ) -> Any:
        user = self._get_user(user_id)
        res_id = next((rid for rid in ("HATHAT", "HATHAU", "HATHAV") if rid not in self.db["reservations"]), None)
        if res_id is None:
            raise ValueError("Too many reservations")
        res = {
            "reservation_id": res_id, "user_id": user_id, "origin": origin, "destination": destination,
            "flight_type": flight_type, "cabin": cabin, "flights": [], "passengers": deepcopy(passengers),
            "payment_history": deepcopy(payment_methods), "created_at": "2024-05-15T15:00:00",
            "total_baggages": total_baggages, "nonfree_baggages": nonfree_baggages, "insurance": insurance,
        }
        total_price = 0
        insts = []
        for fi in flights:
            fl = self._get_flight(fi["flight_number"])
            inst = self._get_flight_instance(fi["flight_number"], fi["date"])
            if inst["status"] != "available":
                raise ValueError(f"Flight {fi['flight_number']} not available on date {fi['date']}")
            if inst["available_seats"][cabin] < len(passengers):
                raise ValueError(f"Not enough seats on flight {fi['flight_number']}")
            price = inst["prices"][cabin]
            res["flights"].append({"origin": fl["origin"], "destination": fl["destination"], "flight_number": fi["flight_number"], "date": fi["date"], "price": price})
            insts.append(inst)
            total_price += price * len(passengers)
        if insurance == "yes":
            total_price += 30 * len(passengers)
        total_price += 50 * nonfree_baggages
        for pm in payment_methods:
            pid = pm["payment_id"]
            if pid not in user["payment_methods"]:
                raise ValueError(f"Payment method {pid} not found")
            upm = user["payment_methods"][pid]
            if upm["source"] in ("gift_card", "certificate") and upm["amount"] < pm["amount"]:
                raise ValueError(f"Not enough balance in payment method {pid}")
        if sum(p["amount"] for p in payment_methods) != total_price:
            raise ValueError(f"Payment amount does not add up, total price is {total_price}, but paid {sum(p['amount'] for p in payment_methods)}")
        for pm in payment_methods:
            pid = pm["payment_id"]
            upm = user["payment_methods"][pid]
            if upm["source"] == "gift_card":
                upm["amount"] -= pm["amount"]
            elif upm["source"] == "certificate":
                user["payment_methods"].pop(pid)
        for inst in insts:
            inst["available_seats"][cabin] -= len(passengers)
        self.db["reservations"][res_id] = res
        user["reservations"].append(res_id)
        return deepcopy(res)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().replace(",", "")).strip()


def run_suite(
    data_dir: Path,
    limit: int = 12,
    max_steps: int = 24,
    model: str = "deepseek-chat",
    workers: int = 4,
) -> Dict[str, Any]:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY environment variable is required")

    base_db = json.loads((data_dir / "db.json").read_text(encoding="utf-8"))
    policy = (data_dir / "policy.md").read_text(encoding="utf-8")
    all_tasks = json.loads((data_dir / "tasks.json").read_text(encoding="utf-8"))
    tasks = all_tasks[:limit] if limit > 0 else all_tasks

    kit_lock = threading.Lock()
    kits: Dict[int, AirlineToolKit] = {}

    for schema in TOOL_SCHEMAS:
        tname = schema["function"]["name"]

        def _make_handler(name: str):
            def _handler(params: Dict[str, Any]) -> Any:
                p = dict(params or {})
                kid = int(p.pop("_kit_id", 0))
                with kit_lock:
                    kit = kits.get(kid)
                assert kit is not None
                return kit.dispatch(name, p)
            return _handler

        AutoTool.register(f"tau2/{tname}", _make_handler(tname), schema["function"]["description"])

    model_service = ModelService()
    model_service.request(
        "save",
        {
            "id": "deepseek_tau2",
            "label": "DeepSeek Tau2",
            "provider": "deepseek",
            "base_url": "https://api.deepseek.com/v1",
            "model": model,
            "key": api_key,
        },
    )
    model_service.request("select", {"id": "deepseek_tau2"})

    rows: List[Dict[str, Any]] = []
    total_llm_calls = 0
    total_tool_calls = 0
    total_tokens = 0
    t_suite0 = time.perf_counter()

    with tempfile.TemporaryDirectory() as store_tmp:
        with AIOSKernelRuntime(
            scheduler_policy="fifo",
            storage_root=str(Path(store_tmp) / "store"),
            log_mode="silent",
        ) as kernel:
            kernel.llm.model_service = model_service
            kernel.scheduler.drf_controller.global_max_model_calls = max(1000, len(tasks) * 40)
            kernel.scheduler.drf_controller.global_max_tokens = 5_000_000

            stats_lock = threading.Lock()

            def _run_one(idx_task: Tuple[int, Dict[str, Any]]) -> Dict[str, Any]:
                nonlocal total_llm_calls, total_tool_calls, total_tokens
                idx, task = idx_task
                t0 = time.perf_counter()
                kit = AirlineToolKit(deepcopy(base_db))
                with kit_lock:
                    kits[idx] = kit

                agent_id = f"tau2_agent_{idx}"
                user_id = f"tau2_user_{idx}"

                instr = task["user_scenario"]["instructions"]
                user_sys = (
                    "You are simulating a customer talking to an airline customer service agent.\n"
                    f"Known info:\n{instr.get('known_info') or ''}\n\n"
                    f"Reason for call:\n{instr.get('reason_for_call') or ''}\n\n"
                    f"Task instructions:\n{instr.get('task_instructions') or ''}\n\n"
                    "Follow your scenario faithfully. Reveal information naturally as needed. "
                    "When your request has been fully resolved OR the agent definitively refuses/transfers you and no further progress can be made, "
                    "end your reply with ###STOP###."
                )
                agent_sys = (
                    "You are a helpful airline customer service agent. You MUST strictly follow all domain policies below.\n"
                    "Only perform database-modifying actions after verifying eligibility and obtaining user confirmation when required.\n\n"
                    f"{policy}"
                )

                user_msgs: List[Dict[str, Any]] = [
                    {"role": "system", "content": user_sys},
                    {"role": "user", "content": "Hi! How can I help you today?"},
                ]
                agent_msgs: List[Dict[str, Any]] = [
                    {"role": "system", "content": agent_sys},
                    {"role": "assistant", "content": "Hi! How can I help you today?"},
                ]
                agent_replies: List[str] = []
                steps = 0
                terminated = "max_steps"

                try:
                    with kernel.task_scope(agent_id, max_model_calls=50, max_tokens=250_000) as a_scope, \
                         kernel.task_scope(user_id, max_model_calls=50, max_tokens=250_000) as u_scope:
                        for step in range(max_steps):
                            steps = step + 1
                            # 1. User simulator turn
                            u_res = u_scope.execute(
                                LLMQuery(model=model, messages=user_msgs, max_new_tokens=512, temperature=0.0),
                            )
                            u_resp = u_res["response"]
                            with stats_lock:
                                total_llm_calls += 1
                                total_tokens += int((getattr(u_resp, "usage", None) or {}).get("total_tokens", 0) or 0)
                            u_text = str(u_resp.response_message or "").strip()
                            if u_resp.status_code != 200 or not u_text:
                                terminated = f"user_err_{u_resp.status_code}"
                                break
                            user_msgs.append({"role": "assistant", "content": u_text})
                            clean_u = u_text.replace("###STOP###", "").strip()
                            if "###STOP###" in u_text and not clean_u:
                                terminated = "user_stop"
                                break
                            agent_msgs.append({"role": "user", "content": clean_u or u_text})
                            if "###STOP###" in u_text and step > 0:
                                terminated = "user_stop"
                                break

                            # 2. Agent loop (may make multiple tool calls before replying to user)
                            for _ in range(6):
                                a_res = a_scope.execute(
                                    LLMQuery(model=model, messages=agent_msgs, tools=TOOL_SCHEMAS, max_new_tokens=1536, temperature=0.0),
                                )
                                a_resp = a_res["response"]
                                with stats_lock:
                                    total_llm_calls += 1
                                    total_tokens += int((getattr(a_resp, "usage", None) or {}).get("total_tokens", 0) or 0)
                                if a_resp.status_code != 200:
                                    terminated = f"agent_err_{a_resp.status_code}"
                                    break
                                tcalls = a_resp.tool_calls or []
                                if tcalls:
                                    norm_calls = []
                                    for idx_tc, tc in enumerate(tcalls):
                                        fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                                        tc_name = tc.get("name") or fn.get("name") or ""
                                        raw_args = tc.get("parameters") if "parameters" in tc else fn.get("arguments", "{}")
                                        if isinstance(raw_args, str):
                                            try:
                                                parsed_args = json.loads(raw_args) if raw_args.strip() else {}
                                            except json.JSONDecodeError:
                                                parsed_args = {}
                                        elif isinstance(raw_args, dict):
                                            parsed_args = raw_args
                                        else:
                                            parsed_args = {}
                                        norm_calls.append({
                                            "id": tc.get("id") or f"call_{idx}_{step}_{idx_tc}",
                                            "name": tc_name,
                                            "parameters": parsed_args,
                                        })
                                    assistant_msg: Dict[str, Any] = {
                                        "role": "assistant",
                                        "content": a_resp.response_message or "",
                                        "tool_calls": [
                                            {
                                                "id": nc["id"],
                                                "type": "function",
                                                "function": {
                                                    "name": nc["name"],
                                                    "arguments": json.dumps(nc["parameters"], ensure_ascii=False),
                                                },
                                            }
                                            for nc in norm_calls
                                        ],
                                    }
                                    agent_msgs.append(assistant_msg)
                                    for nc in norm_calls:
                                        call_params = dict(nc["parameters"])
                                        call_params["_kit_id"] = idx
                                        tr = a_scope.execute(
                                            ToolQuery(tool_calls=[{"name": f"tau2/{nc['name']}", "parameters": call_params}]),
                                        )
                                        with stats_lock:
                                            total_tool_calls += 1
                                        t_resp = tr["response"]
                                        if t_resp.status_code == 200:
                                            content_str = json.dumps(t_resp.response_message, ensure_ascii=False)
                                        else:
                                            content_str = f"Error: {t_resp.error}"
                                        agent_msgs.append({"role": "tool", "tool_call_id": nc["id"], "name": nc["name"], "content": content_str})
                                    if kit.transferred:
                                        terminated = "transferred"
                                        break
                                else:
                                    reply = str(a_resp.response_message or "").strip()
                                    agent_msgs.append({"role": "assistant", "content": reply})
                                    agent_replies.append(reply)
                                    user_msgs.append({"role": "user", "content": reply})
                                    break
                            if terminated in ("transferred", "user_stop") or terminated.startswith("agent_err"):
                                break

                    # Official DB hash + communicate_info grading
                    golden_kit = AirlineToolKit(deepcopy(base_db))
                    for act in task.get("evaluation_criteria", {}).get("actions") or []:
                        try:
                            golden_kit.dispatch(act["name"], act.get("arguments") or {})
                        except Exception:
                            pass
                    db_match = kit.db_hash() == golden_kit.db_hash()
                    comm_list = task.get("evaluation_criteria", {}).get("communicate_info") or []
                    all_agent_text = _normalize("\n".join(agent_replies))
                    comm_match = all(_normalize(str(item)) in all_agent_text for item in comm_list)
                    passed = bool(db_match and comm_match)
                    elapsed = round(time.perf_counter() - t0, 2)
                    row = {
                        "task_id": str(task["id"]),
                        "passed": passed,
                        "db_match": db_match,
                        "comm_match": comm_match,
                        "steps": steps,
                        "terminated": terminated,
                        "seconds": elapsed,
                    }
                    print(
                        f"[{idx+1:02d}/{len(tasks)}] id={task['id']} {'PASS' if passed else 'FAIL'} "
                        f"db={db_match} comm={comm_match} steps={steps} term={terminated} time={elapsed}s",
                        flush=True,
                    )
                    return row
                finally:
                    with kit_lock:
                        kits.pop(idx, None)

            if workers > 1:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    rows = list(pool.map(_run_one, list(enumerate(tasks))))
            else:
                rows = [_run_one(item) for item in enumerate(tasks)]
            rows.sort(key=lambda r: int(r["task_id"]))

            total_syscalls = kernel.syscall_executor.id
    model_service.close()

    passed_count = sum(1 for r in rows if r["passed"])
    return {
        "benchmark": "sierra-research/tau2-bench (~2.5k stars) Airline Domain (pass^1)",
        "runtime": "AIOS Formal Kernel (LLMQuery + ToolQuery) + AirlineToolKit",
        "model": model,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "summary": {
            "tasks_evaluated": len(rows),
            "tasks_passed": passed_count,
            "pass_1_rate": round(passed_count / max(1, len(rows)), 4),
            "score_percent": round(100.0 * passed_count / max(1, len(rows)), 2),
            "db_match_count": sum(1 for r in rows if r["db_match"]),
            "comm_match_count": sum(1 for r in rows if r["comm_match"]),
            "total_llm_calls": total_llm_calls,
            "total_tool_calls": total_tool_calls,
            "total_kernel_syscalls": total_syscalls,
            "total_tokens": total_tokens,
            "wall_seconds": round(time.perf_counter() - t_suite0, 2),
        },
        "tasks": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("/home/tzd/tau2_airline"))
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--max-steps", type=int, default=16)
    parser.add_argument("--model", default="deepseek-chat")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "reports" / "tau2_airline_deepseek.json")
    args = parser.parse_args()

    report = run_suite(data_dir=args.data_dir, limit=args.limit, max_steps=args.max_steps, model=args.model, workers=args.workers)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print("TAU2_AIRLINE_SUMMARY " + json.dumps(report["summary"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
