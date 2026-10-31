#!/usr/bin/env python3
"""Python 3.10+ standard-library local demo. Bind to loopback only."""
import argparse
import base64
import hashlib
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from first_connection.catalog import load_catalog
from first_connection.engine import profile_from, read_regional_signals, run_rules, today_kst
from first_connection.llm import run_llm
from first_connection.clarify import propose_constraints, apply_choices
from first_connection.analysis_link import build_analysis_context, attach_analysis_context
from first_connection.analysis_artifacts import load_latest_analysis
from first_connection.activity_assessment import load_activity_model, attach_activity_assessment

ROOT = Path(__file__).resolve().parent


def content_security_policy():
    """Allow only the exact embedded app script, preserving the local CSP."""
    html = (ROOT / "static/index.html").read_text(encoding="utf-8")
    scripts = re.findall(r'<script id="app-code">([\s\S]*?)</script>', html)
    script_src = "'self'"
    if len(scripts) == 1:
        digest = base64.b64encode(hashlib.sha256(scripts[0].encode("utf-8")).digest()).decode("ascii")
        script_src += f" 'sha256-{digest}'"
    return (f"default-src 'self'; style-src 'self'; script-src {script_src}; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # No profile text, request body, or query string in access logs.
        pass

    def send(self, status, body, mime="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        if isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", content_security_policy())
        self.end_headers()
        self.wfile.write(body)

    def valid_host(self):
        return self.headers.get("Host") in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

    def do_GET(self):
        if not self.valid_host():
            return self.send(403, {"error": "로컬 호스트에서만 이용할 수 있습니다."})
        if self.path == "/api/meta":
            catalog = load_catalog()
            manifest = json.loads((ROOT / "data/manifest.json").read_text())
            return self.send(200, {"version": "0.10", "analysis_available": load_latest_analysis()['available'], "activity_model_available": load_activity_model()['available'], "llm_live_api_verification": "not_verified", "today": today_kst().isoformat(), "sources": sum(s['status']=='collected' for s in manifest),
                "services": len(catalog["services"]), "research_only": sum(s["use_status"] == "research_only" for s in catalog["services"]),
                "built_at": catalog["built_at"], "llm_configured": bool(os.environ.get("OPENAI_API_KEY") and os.environ.get("OPENAI_MODEL")),
                "model": os.environ.get("OPENAI_MODEL")})
        if self.path == "/api/analysis/latest":
            return self.send(200, load_latest_analysis())
        if self.path == "/api/activity/latest":
            return self.send(200, load_activity_model())
        assets = {"/": ("index.html", "text/html; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8")}
        if self.path in assets:
            name, mime = assets[self.path]
            return self.send(200, (ROOT / "static" / name).read_bytes(), mime)
        return self.send(404, {"error": "찾을 수 없습니다."})

    def do_POST(self):
        if not self.valid_host():
            return self.send(403, {"error": "허용되지 않은 호스트입니다."})
        origin = self.headers.get("Origin")
        if origin and origin != "http://" + self.headers.get("Host", ""):
            return self.send(403, {"error": "다른 사이트의 요청은 허용하지 않습니다."})
        try:
            if self.headers.get_content_type() != "application/json":
                raise ValueError("JSON 요청만 지원합니다.")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 200000:
                raise ValueError("요청은 200KB 이내여야 합니다.")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("JSON 객체를 입력하세요.")
            if self.path == "/api/signals":
                text = data.get("csv", "")
                if not isinstance(text, str):
                    raise ValueError("CSV 텍스트가 필요합니다.")
                return self.send(200, {"signals": read_regional_signals(text), "scope": "지역·계층 단위의 참고 지표이며 개인 판정에 사용하지 않음"})
            if self.path == "/api/clarify":
                profile = profile_from(data.get("profile"))
                return self.send(200, {"suggestions": propose_constraints(profile), "method": "명시적 문장 조건 및 선택한 지원 희망의 확인 규칙. 수락 전에는 조건을 변경하지 않음."})
            if self.path != "/api/plan":
                return self.send(404, {"error": "찾을 수 없습니다."})
            profile = profile_from(data.get("profile"))
            profile, clarification_record = apply_choices(profile, data.get("clarification_choices"))
            analysis_context = build_analysis_context(data.get("analysis_csv", ""), profile)
            activity_enabled = data.get('use_activity_model', False)
            if type(activity_enabled) is not bool:
                raise ValueError('활동 모형 연결은 불리언이어야 합니다.')
            activity_model = None
            if activity_enabled:
                analysis = load_latest_analysis()
                activity_model = load_activity_model()
                if not analysis['available'] or not activity_model['available']:
                    raise ValueError('완료·검증된 분석과 활동 모형이 필요합니다.')
                if data.get('analysis_csv') != analysis['csv']:
                    raise ValueError('활동 모형은 기본 제공 데이터 분석과 함께 연결해야 합니다.')
            mode = data.get("mode", "rules")
            if mode not in {"rules", "llm"}:
                raise ValueError("지원하지 않는 실행 모드입니다.")
            if mode == "llm" and data.get("external_processing_consent") is not True:
                raise ValueError("LLM 모드는 입력 정보의 외부 API 전송 선택이 필요합니다.")
            run = run_llm if mode == "llm" else run_rules
            plan = run(profile, data.get("feedback"))
            attach_analysis_context(plan, analysis_context)
            if activity_model:
                attach_activity_assessment(plan, activity_model)
            if clarification_record:
                plan["trace"].insert(0, {"tool": "confirm_constraints", "result": clarification_record})
            return self.send(200, plan)
        except (ValueError, TypeError, KeyError) as exc:
            return self.send(400, {"error": str(exc)})
        except Exception:
            return self.send(500, {"error": "실행에 실패했습니다. 서버 설정과 카탈로그를 확인하세요."})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    load_catalog()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"첫연결 → http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
