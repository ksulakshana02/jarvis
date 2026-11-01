"""The 'brain' for lite mode: turns a user sentence into tool calls + a spoken reply.

Backends:
  ollama  - local LLM via Ollama (free, offline, no quota)         LLM_BACKEND=ollama
  openai  - any OpenAI-compatible server (LM Studio, llama.cpp,
            Groq, OpenRouter...)                                    LLM_BACKEND=openai
  gemini  - Google AI Studio (Flash-Lite, quota)                    LLM_BACKEND=gemini
LLM_FALLBACK lists backends tried in order if the main one fails (e.g. Ollama not running).
"""
import base64
import asyncio
import json
import re

import config
import tools
from live_session import system_prompt

EXTRA = ("\nYou cannot browse the internet yourself; use search_web/open_website for lookups."
         "\nAlways answer in one or two short spoken sentences.")
if config.REPLY_LANGUAGE == "si":
    EXTRA += ("\nALWAYS reply in Sinhala, written in Sinhala script (සිංහල), even if the user speaks English."
              " Keep names of apps, websites and songs as they are.")


def _lower_schema(s):
    """Gemini-style schema (OBJECT/STRING) -> JSON schema (object/string)."""
    if isinstance(s, dict):
        return {k: (v.lower() if k == "type" and isinstance(v, str) else _lower_schema(v)) for k, v in s.items()}
    if isinstance(s, list):
        return [_lower_schema(x) for x in s]
    return s


OPENAI_TOOLS = [{"type": "function", "function": {
    "name": d["name"], "description": d["description"],
    "parameters": _lower_schema(d.get("parameters", {"type": "OBJECT", "properties": {}})),
}} for d in tools.DECLARATIONS]


class Brain:
    name = "base"
    max_rounds = 5

    def __init__(self, on_tool=None):
        self.on_tool = on_tool or (lambda name, args: None)

    def run_tool(self, name, args):
        self.tools_used = True
        self.on_tool(name, args)
        print(f"[tool] {name}({args})")
        return tools.call(name, args)

    def chat(self, text: str) -> str:  # blocking; called in a thread
        raise NotImplementedError


# ---------------------------------------------------------------- OpenAI-compatible (Ollama etc.)
class OpenAIBrain(Brain):
    def __init__(self, base_url, api_key, model, vision=False, on_tool=None, name="openai"):
        super().__init__(on_tool)
        from openai import OpenAI
        # Short timeout, no retry: if the local model is too slow, fall back quickly instead of hanging.
        self.client = OpenAI(base_url=base_url, api_key=api_key or "none",
                             timeout=config.LLM_TIMEOUT, max_retries=0)
        self.model, self.vision, self.name = model, vision, name
        self.history = []
        self.system = {"role": "system", "content": system_prompt() + EXTRA}
        # Ollama ignores "/no_think"; reasoning_effort=none really turns off Qwen3/DeepSeek thinking.
        self.extra = {"reasoning_effort": "none"} if name.startswith("ollama") else {}

    def check(self):
        self.client.models.list()  # raises if server is down

    def warmup(self):
        """Load the model into RAM now so the first command is fast."""
        self.client.with_options(timeout=300).chat.completions.create(
            model=self.model, max_tokens=1, tools=OPENAI_TOOLS, extra_body=self.extra,
            messages=[self.system, {"role": "user", "content": "hi"}])

    def chat(self, text):
        self.history.append({"role": "user", "content": text})
        reply = ""
        for _ in range(self.max_rounds):
            r = self.client.chat.completions.create(
                model=self.model, messages=[self.system] + self.history, tools=OPENAI_TOOLS,
                temperature=0.4, max_tokens=400, extra_body=self.extra,
            )
            msg = r.choices[0].message
            content = re.sub(r"<think>.*?</think>", "", msg.content or "", flags=re.S).strip()
            calls = msg.tool_calls or []
            entry = {"role": "assistant", "content": content}
            if calls:
                entry["tool_calls"] = [{"id": c.id, "type": "function",
                                        "function": {"name": c.function.name, "arguments": c.function.arguments}}
                                       for c in calls]
            self.history.append(entry)
            reply = content or reply
            if not calls:
                break
            for c in calls:
                try:
                    args = json.loads(c.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                out = self.run_tool(c.function.name, args)
                img = out.pop("_image", None) if isinstance(out, dict) else None
                if img and not self.vision:
                    out = {"result": "Screen captured, but this local model can't see images. Tell the user."}
                self.history.append({"role": "tool", "tool_call_id": c.id, "content": json.dumps(out, default=str)})
                if img and self.vision:
                    self.history.append({"role": "user", "content": [
                        {"type": "text", "text": "Here is the screenshot."},
                        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(img).decode()}},
                    ]})
        self._trim()
        return reply

    def _trim(self):
        h = self.history[-config.HISTORY_TURNS:]
        while h and h[0]["role"] != "user":
            h.pop(0)
        self.history = h


# ---------------------------------------------------------------- Gemini
class GeminiBrain(Brain):
    name = "gemini"

    def __init__(self, on_tool=None):
        super().__init__(on_tool)
        # Lite mode runs the blocking brain in a worker thread. The synchronous
        # Gemini client still expects that thread to have an event loop.
        try:
            asyncio.get_event_loop()
        except RuntimeError:
            asyncio.set_event_loop(asyncio.new_event_loop())
        from google import genai
        from google.genai import types
        self.types = types
        self.client = genai.Client(api_key=config.API_KEY)
        self.models = [config.TEXT_MODEL] + [m for m in config.TEXT_FALLBACKS if m != config.TEXT_MODEL]
        self.mi = 0
        self.history = []

    def check(self):
        if not config.API_KEY:
            raise RuntimeError("no GEMINI_API_KEY")

    def _config(self):
        t = self.types
        return t.GenerateContentConfig(
            system_instruction=system_prompt() + EXTRA,
            tools=[t.Tool(function_declarations=tools.DECLARATIONS)],
            automatic_function_calling=t.AutomaticFunctionCallingConfig(disable=True),
            temperature=0.6, max_output_tokens=400,
        )

    def _generate(self):
        last = None
        for _ in range(len(self.models)):
            model = self.models[self.mi]
            try:
                return self.client.models.generate_content(model=model, contents=self.history, config=self._config())
            except Exception as e:
                last, msg = e, str(e).lower()
                if any(k in msg for k in ("quota", "resource_exhausted", "429", "not found", "404")):
                    print(f"[gemini] {model}: {str(e)[:90]} -> next model")
                    self.mi = (self.mi + 1) % len(self.models)
                    continue
                raise
        raise last

    def chat(self, text):
        t = self.types
        self.history.append(t.Content(role="user", parts=[t.Part(text=text)]))
        reply = ""
        for _ in range(self.max_rounds):
            resp = self._generate()
            cand = resp.candidates[0].content if resp.candidates else None
            if not cand or not cand.parts:
                break
            self.history.append(cand)
            calls = [p.function_call for p in cand.parts if p.function_call]
            reply = " ".join(p.text for p in cand.parts if p.text and not getattr(p, "thought", False)).strip() or reply
            if not calls:
                break
            parts, images = [], []
            for fc in calls:
                out = self.run_tool(fc.name, dict(fc.args or {}))
                if isinstance(out, dict) and "_image" in out:
                    images.append(out.pop("_image"))
                parts.append(t.Part(function_response=t.FunctionResponse(id=fc.id, name=fc.name, response=out)))
            parts += [t.Part(inline_data=t.Blob(data=i, mime_type="image/jpeg")) for i in images]
            self.history.append(t.Content(role="user", parts=parts))
        h = self.history[-config.HISTORY_TURNS:]
        while h and (h[0].role != "user" or any(p.function_response for p in h[0].parts or [])):
            h.pop(0)
        self.history = h
        return reply


# ---------------------------------------------------------------- factory + fallback
def make(backend, on_tool):
    if backend == "ollama":
        return OpenAIBrain(config.OLLAMA_URL.rstrip("/") + "/v1", "ollama", config.OLLAMA_MODEL,
                           vision=config.LLM_VISION, on_tool=on_tool, name=f"ollama:{config.OLLAMA_MODEL}")
    if backend == "openai":
        return OpenAIBrain(config.OPENAI_BASE_URL, config.OPENAI_API_KEY, config.OPENAI_MODEL,
                           vision=config.LLM_VISION, on_tool=on_tool, name=f"openai:{config.OPENAI_MODEL}")
    if backend == "gemini":
        return GeminiBrain(on_tool)
    raise ValueError(f"unknown LLM backend {backend}")


class FallbackBrain:
    """Tries backends in order; sticks with the first that works."""

    def __init__(self, on_tool):
        self.order = [config.LLM_BACKEND] + [b for b in config.LLM_FALLBACK if b != config.LLM_BACKEND]
        self.on_tool = on_tool
        self.cache = {}
        self.active = None

    def _get(self, b):
        if b not in self.cache:
            brain = make(b, self.on_tool)
            brain.check()
            self.cache[b] = brain
        return self.cache[b]

    def warmup(self):
        try:
            b = self._get(self.order[0])
            if hasattr(b, "warmup"):
                print(f"[brain] loading {b.name} ...")
                b.warmup()
                print(f"[brain] {b.name} ready")
        except Exception as e:
            print(f"[brain] {self.order[0]} not available ({str(e)[:80]}); will use {self.order[1:]}")

    def chat(self, text):
        errors = []
        order = ([self.active] if self.active else []) + [x for x in self.order if x != self.active]
        sinhala = bool(re.search("[\u0D80-\u0DFF]", text))
        if sinhala and "gemini" in order:
            # small local models can't read or write Sinhala (they reply with gibberish or nothing)
            order = ["gemini"] + [x for x in order if x != "gemini"]
        for b in order:
            try:
                brain = self._get(b)
                brain.tools_used = False
                reply = brain.chat(text)
                if not reply.strip() and not brain.tools_used:
                    raise RuntimeError("empty reply")  # try the next brain instead of saying "Done."
                if self.active != b:
                    print(f"[brain] using {brain.name}")
                if not sinhala:  # a Sinhala detour to Gemini shouldn't change the brain for English turns
                    self.active = b
                return reply
            except Exception as e:
                errors.append(f"{b}: {str(e)[:120]}")
                print(f"[brain] {b} failed: {str(e)[:160]}")
                self.cache.pop(b, None)
                if self.active == b:
                    self.active = None
        raise RuntimeError(" | ".join(errors))
