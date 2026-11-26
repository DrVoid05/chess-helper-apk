import json
import os
import random
import shutil
import stat
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import chess
import chess.engine

def find_stockfish() -> str:
    here = Path(__file__).resolve().parent
    candidates = [
        here / "stockfish",
        here / "bin" / "stockfish",
        Path("/data/data/com.termux/files/usr/bin/stockfish"),
    ]
    for p in candidates:
        if p.exists() and p.is_file():
            try:
                p.chmod(p.stat().st_mode | stat.S_IEXEC)
            except Exception:
                pass
            return str(p)
    found = shutil.which("stockfish")
    return found or "stockfish"

class ChessService:
    def __init__(self):
        self.lock = threading.Lock()
        self.board = chess.Board()
        self.mode = "analyzer"
        self.side = "both"
        self.elo = 1200
        self.chess960 = False
        self.gen = 0
        self.engine_path = find_stockfish()
        self.engine = None

    def _start_engine(self):
        if self.engine is not None:
            try:
                self.engine.quit()
            except Exception:
                pass
            self.engine = None
        eng = chess.engine.SimpleEngine.popen_uci(self.engine_path)
        try:
            eng.configure({"Threads": 1, "Hash": 16})
        except Exception:
            pass
        self.engine = eng

    def _ensure_engine(self):
        if self.engine is None:
            self._start_engine()

    def _run_timeout(self, fn, sec=1.2):
        res, err = {}, {}
        def work():
            try:
                res["v"] = fn()
            except Exception as e:
                err["e"] = e
        th = threading.Thread(target=work, daemon=True)
        th.start()
        th.join(timeout=sec)
        if th.is_alive():
            self._start_engine()
            raise TimeoutError("timeout")
        if "e" in err:
            raise err["e"]
        return res.get("v")

    def _eval_cp(self, score) -> int:
        s = score.white()
        if s.is_mate():
            m = s.mate() or 0
            return 10000 if m > 0 else -10000
        return int(s.score() or 0)

    def _fmt_score(self, score) -> str:
        s = score.white()
        if s.is_mate():
            m = s.mate() or 0
            return f"M{m}" if m > 0 else f"-M{abs(m)}"
        return f"{(s.score() or 0)/100.0:+.2f}"

    def _pv_san(self, pv) -> str:
        tmp = self.board.copy()
        out = []
        for mv in (pv or [])[:4]:
            if mv in tmp.legal_moves:
                out.append(tmp.san(mv))
                tmp.push(mv)
        return " ".join(out)

    def payload(self) -> dict:
        return {
            "fen": self.board.fen(),
            "turn": "white" if self.board.turn == chess.WHITE else "black",
            "mode": self.mode,
            "side": self.side,
            "elo": self.elo,
            "chess960": self.chess960,
            "gen": self.gen,
            "legal": [m.uci() for m in self.board.legal_moves],
            "last": self.board.peek().uci() if self.board.move_stack else None,
            "game_over": self.board.is_game_over(),
            "result": self.board.result() if self.board.is_game_over() else "",
        }

    def _bot_move_locked(self):
        if self.board.is_game_over():
            return
        moves = list(self.board.legal_moves)
        if not moves:
            return
        try:
            self._ensure_engine()
            res = self._run_timeout(
                lambda: self.engine.analyse(
                    self.board, chess.engine.Limit(depth=6, time=0.06), multipv=min(3, len(moves))
                ),
                1.0,
            )
            rows = res if isinstance(res, list) else [res]
            if rows:
                if self.elo <= 800:
                    pick = rows[-1]
                elif self.elo <= 1600:
                    pick = rows[len(rows) // 2]
                else:
                    pick = rows[0]
                pv = pick.get("pv") or []
                if pv and pv[0] in self.board.legal_moves:
                    self.board.push(pv[0])
                    self.gen += 1
                    return
        except Exception:
            self._start_engine()
        self.board.push(random.choice(moves))
        self.gen += 1

    def reset(self, mode=None, side=None, elo=None, chess960=None) -> dict:
        with self.lock:
            if mode in ("analyzer", "trainer"):
                self.mode = mode
            if side in ("white", "black", "both"):
                self.side = side
            if elo is not None:
                self.elo = int(elo)
            if chess960 is not None:
                self.chess960 = bool(chess960)
            if self.chess960:
                self.board = chess.Board.from_chess960_pos(random.randint(0, 959))
            else:
                self.board = chess.Board()
            self.gen += 1
            if self.mode == "trainer" and self.side == "black" and self.board.turn == chess.WHITE:
                self._bot_move_locked()
            return self.payload()

    def move(self, uci: str) -> dict:
        with self.lock:
            try:
                mv = chess.Move.from_uci(uci)
            except Exception:
                return self.payload()
            if mv in self.board.legal_moves:
                self.board.push(mv)
                self.gen += 1
                if self.mode == "trainer" and not self.board.is_game_over():
                    if (self.side == "white" and self.board.turn == chess.BLACK) or (
                        self.side == "black" and self.board.turn == chess.WHITE
                    ):
                        self._bot_move_locked()
            return self.payload()

    def undo(self) -> dict:
        with self.lock:
            if self.board.move_stack:
                self.board.pop()
                if self.mode == "trainer" and self.board.move_stack:
                    self.board.pop()
                self.gen += 1
            return self.payload()

    def analyze(self) -> dict:
        with self.lock:
            gen_now = self.gen
            moves = list(self.board.legal_moves)
            if self.board.is_game_over() or not moves:
                return {"gen": gen_now, "cp": 0, "score": "0.00", "lines": []}
            mpv = min(3, len(moves))
            try:
                self._ensure_engine()
                res = self._run_timeout(
                    lambda: self.engine.analyse(
                        self.board, chess.engine.Limit(depth=7, time=0.08), multipv=mpv
                    ),
                    1.2,
                )
                rows = res if isinstance(res, list) else [res]
                lines, top_cp = [], 0
                for i, item in enumerate(rows):
                    if not isinstance(item, dict):
                        continue
                    pv = item.get("pv") or []
                    if not pv or pv[0] not in self.board.legal_moves:
                        continue
                    sc = item.get("score")
                    if i == 0 and sc:
                        top_cp = self._eval_cp(sc)
                    mv0 = pv[0]
                    lines.append({
                        "uci": mv0.uci(),
                        "san": self.board.san(mv0),
                        "score": self._fmt_score(sc) if sc else "0.00",
                        "pv": self._pv_san(pv),
                    })
                return {"gen": gen_now, "cp": top_cp, "score": lines[0]["score"] if lines else "0.00", "lines": lines}
            except Exception:
                mv0 = moves[0]
                return {
                    "gen": gen_now,
                    "cp": 0,
                    "score": "0.00",
                    "lines": [{"uci": mv0.uci(), "san": self.board.san(mv0), "score": "0.00", "pv": self.board.san(mv0)}],
                }

HTML = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>Chess Helper</title>
<style>
  * { box-sizing: border-box; }
  body { margin: 0; padding: 10px; background: #141312; color: #f2efe9; font-family: sans-serif; }
  .box { max-width: 500px; margin: 0 auto; display: flex; flex-direction: column; gap: 8px; }
  .row { display: flex; gap: 6px; }
  button, select { padding: 10px; border-radius: 8px; border: 1px solid #444; background: #262422; color: #fff; font-size: 14px; font-weight: 600; }
  button.on { background: #3b6ea5; border-color: #5b8ec7; }
  button.blue { background: #2563eb; border-color: #3b82f6; }
  .eval-bar { height: 10px; background: #27272a; border-radius: 6px; overflow: hidden; border: 1px solid #3f3f46; }
  .eval-fill { height: 100%; width: 50%; background: #f4f4f5; transition: width 0.2s; }
  .info { background: #1e1c1a; padding: 8px 12px; border-radius: 8px; display: flex; justify-content: space-between; font-size: 14px; }
  .elo { color: #f59e0b; font-weight: 700; }
  .bwrap { position: relative; width: 100%; aspect-ratio: 1 / 1; border: 2px solid #444; border-radius: 8px; overflow: hidden; }
  .board { display: grid; grid-template-columns: repeat(8, 1fr); grid-template-rows: repeat(8, 1fr); width: 100%; height: 100%; }
  .sq { display: flex; align-items: center; justify-content: center; font-size: clamp(22px, 8vw, 44px); user-select: none; position: relative; }
  .l { background: #f0d9b5; color: #111; }
  .d { background: #b58863; color: #111; }
  .sel { box-shadow: inset 0 0 0 3px #16a34a; }
  .last { background-color: rgba(250, 204, 21, 0.45) !important; }
  .dot { width: 13px; height: 13px; border-radius: 50%; background: rgba(22, 163, 74, 0.85); }
  svg.arrows { position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none; }
  .panel { background: #1e1c1a; border: 1px solid #302d2a; border-radius: 8px; padding: 10px; display: flex; flex-direction: column; gap: 6px; }
  .line { background: #272421; border-radius: 7px; padding: 7px 10px; display: flex; justify-content: space-between; font-size: 13px; }
  .muted { color: #9ca3af; font-size: 13px; }
</style>
</head>
<body>
<div class="box">
  <div class="row">
    <button id="m-ana" style="flex:1" onclick="setMode('analyzer')">🔍 Анализатор</button>
    <button id="m-trn" style="flex:1" onclick="setMode('trainer')">🎓 Тренер</button>
    <button id="m-960" onclick="toggle960()">🎲 960</button>
  </div>
  <div class="eval-bar"><div id="eval-fill" class="eval-fill"></div></div>
  <div class="info">
    <span id="turn">Ход Белых</span>
    <span id="meta" class="elo">Анализатор • 1200 Эло</span>
  </div>
  <div class="bwrap">
    <div id="board" class="board"></div>
    <svg id="arrows" class="arrows" viewBox="0 0 800 800"></svg>
  </div>
  <div class="row">
    <button onclick="doUndo()">↩ Назад</button>
    <button onclick="doFlip()">⇅</button>
    <button class="blue" style="flex:1" onclick="doAnalyze()">⚡ Анализ</button>
    <button onclick="doReset()">⟳ Новая</button>
  </div>
  <div class="row">
    <select id="side" style="flex:1" onchange="doOpts()">
      <option value="both" selected>Сторона: Обе</option>
      <option value="white">Сторона: Белые</option>
      <option value="black">Сторона: Чёрные</option>
    </select>
    <select id="elo" style="flex:1" onchange="doOpts()">
      <option value="400">400 Эло</option>
      <option value="800">800 Эло</option>
      <option value="1200" selected>1200 Эло</option>
      <option value="1600">1600 Эло</option>
      <option value="2000">2000 Эло</option>
      <option value="2500">2500 Эло</option>
      <option value="3000">3000 Эло</option>
    </select>
  </div>
  <div class="panel">
    <div style="color:#34d399;font-weight:700;font-size:13px;">Топ-3 хода и оценка:</div>
    <div id="lines" class="muted">Нажми «⚡ Анализ», чтобы рассчитать позицию</div>
  </div>
</div>
<script>
const GLYPH = {P:"♙",N:"♘",B:"♗",R:"♖",Q:"♕",K:"♔",p:"♟",n:"♞",b:"♝",r:"♜",q:"♛",k:"♚"};
const FILES = "abcdefgh";
let st = null, sel = null, flip = false, anal = null, busy = false;

async function callApi(url, data) {
  try {
    const r = await fetch(url, {
      method: data !== undefined ? "POST" : "GET",
      headers: data !== undefined ? {"Content-Type": "application/json"} : {},
      body: data !== undefined ? JSON.stringify(data) : undefined
    });
    return await r.json();
  } catch (e) { return st || {}; }
}

function gridFromFen(fen) {
  const rows = fen.split(" ")[0].split("/");
  const g = Array.from({length:8}, () => Array(8).fill(null));
  for (let r = 0; r < 8; r++) {
    let c = 0;
    for (const ch of rows[r]) {
      if (ch >= "1" && ch <= "8") c += parseInt(ch, 10);
      else { g[r][c] = ch; c++; }
    }
  }
  return g;
}

function sqToRC(sq) { return { r: 8 - parseInt(sq[1], 10), c: FILES.indexOf(sq[0]) }; }
function rcToSq(r, c) { return FILES[c] + (8 - r); }
function targetsFrom(sq) { return (st && st.legal) ? st.legal.filter(m => m.startsWith(sq)).map(m => m.slice(2, 4)) : []; }

function draw() {
  if (!st || !st.fen) return;
  document.getElementById("m-ana").classList.toggle("on", st.mode === "analyzer");
  document.getElementById("m-trn").classList.toggle("on", st.mode === "trainer");
  document.getElementById("m-960").classList.toggle("on", !!st.chess960);
  document.getElementById("side").value = st.side;
  document.getElementById("elo").value = String(st.elo);
  document.getElementById("turn").textContent = st.game_over ? ("Конец: " + st.result) : (st.turn === "white" ? "Ход Белых" : "Ход Чёрных");
  document.getElementById("meta").textContent = (st.mode === "trainer" ? "Тренер" : "Анализатор") + " • " + st.elo + " Эло";

  const g = gridFromFen(st.fen);
  const tgts = sel ? targetsFrom(sel) : [];
  const lf = st.last ? st.last.slice(0,2) : null;
  const lt = st.last ? st.last.slice(2,4) : null;
  const b = document.getElementById("board");
  b.innerHTML = "";

  for (let vr = 0; vr < 8; vr++) {
    for (let vc = 0; vc < 8; vc++) {
      const r = flip ? 7 - vr : vr;
      const c = flip ? 7 - vc : vc;
      const sq = rcToSq(r, c);
      const p = g[r][c];
      const el = document.createElement("div");
      el.className = "sq " + (((r + c) % 2 === 0) ? "l" : "d");
      if (sq === sel) el.classList.add("sel");
      if (sq === lf || sq === lt) el.classList.add("last");
      if (p) el.textContent = GLYPH[p] || p;
      else if (tgts.includes(sq)) { const d = document.createElement("div"); d.className = "dot"; el.appendChild(d); }
      el.onclick = () => clickSq(sq, p, tgts);
      b.appendChild(el);
    }
  }
  drawArrows();
}

function sqCenter(sq) {
  const { r, c } = sqToRC(sq);
  const vc = flip ? 7 - c : c;
  const vr = flip ? 7 - r : r;
  return { x: vc * 100 + 50, y: vr * 100 + 50 };
}

function drawArrows() {
  const svg = document.getElementById("arrows");
  svg.innerHTML = "";
  if (!anal || !anal.lines) return;
  const cols = ["#22c55e", "#38bdf8", "#f59e0b"];
  anal.lines.slice(0, 3).forEach((ln, idx) => {
    if (!ln.uci || ln.uci.length < 4) return;
    const p1 = sqCenter(ln.uci.slice(0, 2));
    const p2 = sqCenter(ln.uci.slice(2, 4));
    const col = cols[idx] || "#22c55e";
    const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
    line.setAttribute("x1", p1.x); line.setAttribute("y1", p1.y);
    line.setAttribute("x2", p2.x); line.setAttribute("y2", p2.y);
    line.setAttribute("stroke", col); line.setAttribute("stroke-width", idx === 0 ? "12" : "8");
    line.setAttribute("stroke-opacity", "0.8"); line.setAttribute("stroke-linecap", "round");
    svg.appendChild(line);
  });
}

async function clickSq(sq, p, tgts) {
  if (!st || st.game_over || busy) return;
  if (sel && tgts.includes(sq)) {
    let uci = sel + sq;
    if (sq[1] === "8" || sq[1] === "1") {
      const g = gridFromFen(st.fen);
      const rc = sqToRC(sel);
      if (g[rc.r][rc.c] === "P" || g[rc.r][rc.c] === "p") uci += "q";
    }
    sel = null; anal = null;
    busy = true;
    try { st = await callApi("/move", { uci }); } finally { busy = false; }
    draw();
    return;
  }
  if (p) {
    const isBlack = p === p.toLowerCase();
    if ((st.turn === "white" && !isBlack) || (st.turn === "black" && isBlack)) {
      sel = (sel === sq) ? null : sq;
      draw();
      return;
    }
  }
  sel = null; draw();
}

async function doAnalyze() {
  if (busy) return;
  document.getElementById("lines").textContent = "Считаю...";
  busy = true;
  try {
    const res = await callApi("/analyze", {});
    if (st && res.gen === st.gen) {
      anal = res;
      const pct = Math.max(5, Math.min(95, 50 + (res.cp || 0) / 20));
      document.getElementById("eval-fill").style.width = pct + "%";
      document.getElementById("lines").innerHTML = (res.lines || []).map((l, i) =>
        `<div class="line"><span><b>#${i+1} ${l.san}</b> <span class="muted">(${l.pv})</span></span><b>${l.score}</b></div>`
      ).join("") || "Нет вариантов";
      drawArrows();
    }
  } finally { busy = false; }
}

async function setMode(mode) { if (busy) return; sel=null; anal=null; busy=true; try { st = await callApi("/reset", {mode}); } finally { busy=false; } draw(); }
async function toggle960() { if (busy) return; sel=null; anal=null; busy=true; try { st = await callApi("/reset", {chess960: !st.chess960}); } finally { busy=false; } draw(); }
async function doOpts() { if (busy) return; sel=null; anal=null; busy=true; try { st = await callApi("/reset", {side: document.getElementById("side").value, elo: parseInt(document.getElementById("elo").value, 10)}); } finally { busy=false; } draw(); }
async function doReset() { if (busy) return; sel=null; anal=null; busy=true; try { st = await callApi("/reset", {}); } finally { busy=false; } draw(); }
async function doUndo() { if (busy) return; sel=null; anal=null; busy=true; try { st = await callApi("/undo", {}); } finally { busy=false; } draw(); }
function doFlip() { flip = !flip; draw(); }

(async () => { st = await callApi("/state"); draw(); })();
</script>
</body>
</html>
"""

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def _send(self, code, ctype, raw):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try: self.wfile.write(raw)
        except Exception: pass
    def _json(self, obj): self._send(200, "application/json; charset=utf-8", json.dumps(obj, ensure_ascii=False).encode("utf-8"))
    def _body(self):
        n = int(self.headers.get("Content-Length", "0") or "0")
        if n <= 0: return {}
        try: return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception: return {}
    def do_GET(self):
        p = urllib.parse.urlparse(self.path).path
        if p in ("/", "/index.html"): self._send(200, "text/html; charset=utf-8", HTML.encode("utf-8"))
        elif p == "/state":
            with GAME.lock: self._json(GAME.payload())
        else: self._send(404, "text/plain", b"Not Found")
    def do_POST(self):
        p = urllib.parse.urlparse(self.path).path
        b = self._body()
        if p == "/reset": self._json(GAME.reset(b.get("mode"), b.get("side"), b.get("elo"), b.get("chess960")))
        elif p == "/move": self._json(GAME.move(b.get("uci", "")))
        elif p == "/undo": self._json(GAME.undo())
        elif p == "/analyze": self._json(GAME.analyze())
        else: self._send(404, "text/plain", b"Not Found")

GAME = ChessService()

class Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

def run_server():
    srv = Server(("127.0.0.1", 5000), Handler)
    srv.serve_forever()

if __name__ == "__main__":
    print("http://127.0.0.1:5000")
    run_server()
