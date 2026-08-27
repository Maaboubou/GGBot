import tkinter as tk
from tkinter import ttk, scrolledtext
import subprocess
import threading
import sys
import os
import signal
from datetime import datetime
import queue
import psutil
import json
import time
import webbrowser
from dotenv import load_dotenv


_PRESERVED_BROWSER_PROCESSES = {"chrome", "chrome.exe"}
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
_LAUNCHER_STATE_FILE = os.path.join(_PROJECT_ROOT, "data", "launcher_state.json")
_RESTART_SIGNAL_FILE = os.path.join(_PROJECT_ROOT, ".restart_signal")

load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))


def write_launcher_state(state_file=_LAUNCHER_STATE_FILE):
    """公布当前 launcher 支持的重启信号协议，供 Web 后端能力握手。"""
    os.makedirs(os.path.dirname(state_file), exist_ok=True)
    with open(state_file, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "pid": os.getpid(),
                "signal_protocol": 2,
                "started_at": time.time(),
            },
            handle,
        )


def consume_restart_signal(signal_file):
    """读取并删除 Web 重启信号；旧格式和未知内容保持为全部重启。"""
    with open(signal_file, "r", encoding="utf-8") as handle:
        requested_action = handle.read().strip().casefold()
    os.remove(signal_file)
    return "web" if requested_action in {"web", "app", "start.py"} else "all"


def collect_stoppable_processes(root_pid):
    """收集服务进程树，保留可复用的 Chrome，仅清理服务与驱动进程。"""
    try:
        root = psutil.Process(root_pid)
        descendants = root.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return [], 0

    targets = []
    preserved_count = 0
    # 逆序通常会先处理更深层的进程，最后再结束服务根进程。
    for process in reversed(descendants):
        try:
            process_name = process.name().casefold()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            process_name = ""

        if process_name in _PRESERVED_BROWSER_PROCESSES:
            preserved_count += 1
            continue
        targets.append(process)

    targets.append(root)
    return targets, preserved_count

# === 配置 ===
THEME_BG = "#1e1e1e"
THEME_FG = "#cccccc"
THEME_ACCENT = "#0e639c"
THEME_SIDEBAR = "#252526"

SCRIPTS = [
    {"name": "Bot", "file": "wx_bot.py", "color": "#00ffff"},
    {"name": "Web", "file": "start.py", "color": "#00ff00"}
]

class ProcessManager(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title("Mabobot 管理控制台")
        self.geometry("1100x700")
        self.configure(bg=THEME_BG)
        write_launcher_state()

        # 进程存储
        self.processes = {}
        self.stopping_processes = {}
        self.msg_queue = queue.Queue()
        
        # 样式配置
        self.setup_styles()
        
        # 布局
        self.create_layout()
        
        # 启动队列监听循环
        self.check_queue()

        # 优雅退出
        self.protocol("WM_DELETE_WINDOW", self.on_closing)

        # 自动启动
        self.after(500, self.start_all)
        self.after(3000, self.open_console)

    def setup_styles(self):
        style = ttk.Style()
        style.theme_use('clam')
        
        style.configure("TFrame", background=THEME_BG)
        style.configure("Sidebar.TFrame", background=THEME_SIDEBAR)
        
        # 按钮样式
        style.configure("TButton", 
            background=THEME_ACCENT, 
            foreground="white", 
            borderwidth=0, 
            padding=6
        )
        style.map("TButton", 
            background=[('active', '#1177bb'), ('pressed', '#0b4d79')]
        )
        
        style.configure("Danger.TButton", background="#a12626")
        style.map("Danger.TButton", background=[('active', '#cc3333')])

    def create_layout(self):
        # 左侧边栏
        sidebar = ttk.Frame(self, style="Sidebar.TFrame", width=200)
        sidebar.pack(side="left", fill="y")
        
        # 标题
        lbl_title = tk.Label(sidebar, text="Mabobot", bg=THEME_SIDEBAR, fg="white", font=("Arial", 16, "bold"), pady=20)
        lbl_title.pack(fill="x")

        # 状态区
        self.status_indicators = {}
        status_frame = tk.Frame(sidebar, bg=THEME_SIDEBAR, pady=20)
        status_frame.pack(fill="x", padx=20)

        for s in SCRIPTS:
            row = tk.Frame(status_frame, bg=THEME_SIDEBAR)
            row.pack(fill="x", pady=5)
            
            # 指示灯
            canvas = tk.Canvas(row, width=12, height=12, bg=THEME_SIDEBAR, highlightthickness=0)
            dot = canvas.create_oval(2, 2, 10, 10, fill="#666666", outline="")
            canvas.pack(side="left")
            
            lbl = tk.Label(row, text=f" {s['name']} 服务", bg=THEME_SIDEBAR, fg="#aaaaaa", font=("Arial", 10))
            lbl.pack(side="left")
            
            self.status_indicators[s['file']] = (canvas, dot)

        # 按钮区
        btn_frame = tk.Frame(sidebar, bg=THEME_SIDEBAR, pady=20)
        btn_frame.pack(fill="x", padx=15, side="bottom", pady=20)

        btn_start = ttk.Button(btn_frame, text="全部启动", command=self.start_all)
        btn_start.pack(fill="x", pady=5)

        btn_console = ttk.Button(btn_frame, text="打开 Web 控制台", command=self.open_console)
        btn_console.pack(fill="x", pady=5)
        
        btn_restart = ttk.Button(btn_frame, text="重启服务", command=self.restart_all)
        btn_restart.pack(fill="x", pady=5)

        btn_restart_web = ttk.Button(btn_frame, text="只重启 Web", command=self.restart_web)
        btn_restart_web.pack(fill="x", pady=5)

        btn_stop = ttk.Button(btn_frame, text="停止所有", style="Danger.TButton", command=self.stop_all)
        btn_stop.pack(fill="x", pady=5)
        
        btn_clear = ttk.Button(btn_frame, text="清空日志", command=self.clear_log)
        btn_clear.pack(fill="x", pady=5)

        # 右侧日志区
        main_area = ttk.Frame(self, style="TFrame")
        main_area.pack(side="right", fill="both", expand=True)
        
        self.log_widget = scrolledtext.ScrolledText(main_area, bg=THEME_BG, fg=THEME_FG, font=("Consolas", 10), borderwidth=0)
        self.log_widget.pack(fill="both", expand=True, padx=10, pady=10)
        
        # 配置日志颜色标签
        self.log_widget.tag_config("SYS", foreground="#eeee00")
        self.log_widget.tag_config("ERR", foreground="#ff4444")
        for s in SCRIPTS:
            self.log_widget.tag_config(s['name'], foreground=s['color'])

    def open_console(self):
        port = os.getenv("WEB_PORT", "8888").strip() or "8888"
        webbrowser.open(f"http://127.0.0.1:{port}/")

    def log(self, tag, message):
        """将日志放入队列，线程安全"""
        self.msg_queue.put((tag, message))

    def check_queue(self):
        """从队列读取日志并更新 UI"""
        while not self.msg_queue.empty():
            tag, msg = self.msg_queue.get()
            timestamp = datetime.now().strftime("%H:%M:%S")
            
            self.log_widget.configure(state='normal')
            self.log_widget.insert("end", f"[{timestamp}] ", "SYS")
            self.log_widget.insert("end", f"[{tag}] ", tag)
            self.log_widget.insert("end", msg + "\n")
            self.log_widget.configure(state='disabled')
            self.log_widget.see("end")
        
        self.check_restart_signal()
        self.after(100, self.check_queue)

    def check_restart_signal(self):
        """检查是否有重启信号文件"""
        signal_file = _RESTART_SIGNAL_FILE
        if os.path.exists(signal_file):
            try:
                action = consume_restart_signal(signal_file)
                if action == "web":
                    self.log("SYS", f"收到通过 Web 发起的仅重启 Web 请求 ({signal_file})...")
                    self.restart_web()
                else:
                    self.log("SYS", f"收到通过 Web 发起的全部重启请求 ({signal_file})...")
                    self.restart_all()
            except Exception as e:
                self.log("ERR", f"处理重启信号失败: {e}")

    def set_status(self, script_file, is_running):
        canvas, dot = self.status_indicators.get(script_file)
        color = "#00ff00" if is_running else "#666666"
        canvas.itemconfig(dot, fill=color)

    def start_process(self, script_conf):
        file = script_conf['file']
        name = script_conf['name']
        
        if file in self.processes:
            return

        self.log("SYS", f"正在启动 {name} ({file})...")
        
        # 核心修复: 强制 UTF-8 编码，防止 GBK 错误
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        
        process = subprocess.Popen(
            [sys.executable, "-u", os.path.join(_PROJECT_ROOT, file)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=_PROJECT_ROOT,
            # shell=False 更容易管理
            env=env,
            creationflags=subprocess.CREATE_NO_WINDOW # 无黑窗
        )
        
        self.processes[file] = process
        self.set_status(file, True)

        # 读取线程
        def reader(stream, tag):
            try:
                # 使用 utf-8 errors='replace' 来容错
                for line in iter(lambda: stream.readline().decode('utf-8', errors='replace'), ''):
                    if not line: break
                    self.log(tag, line.rstrip())
            except Exception as e:
                self.log("ERR", f"Stream Read Error: {e}")
            finally:
                # 进程结束后更新状态
                if file in self.processes and self.processes[file].poll() is not None:
                    self.after(0, lambda: self.set_status(file, False))
                    self.after(0, lambda: self.clean_process_ref(file))

        t_out = threading.Thread(target=reader, args=(process.stdout, name), daemon=True)
        t_out.start()

    def clean_process_ref(self, file_name):
        if file_name in self.processes:
            del self.processes[file_name]
            self.log("SYS", f"{file_name} 已停止")

    def start_all(self):
        for s in SCRIPTS:
            self.start_process(s)

    def _stop_files(self, script_files):
        selected_files = tuple(script_files)
        for file in selected_files:
            proc = self.processes.get(file)
            if not proc or proc.poll() is not None:
                continue

            targets, preserved_count = collect_stoppable_processes(proc.pid)
            self.stopping_processes[file] = targets

            if targets:
                for target in targets:
                    try:
                        target.terminate()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                child_count = max(0, len(targets) - 1)
                self.log(
                    "SYS",
                    f"正在停止 {file} 及 {child_count} 个子进程"
                    f"（保留 {preserved_count} 个 Chrome 进程）...",
                )
            else:
                try:
                    proc.terminate()
                except OSError:
                    pass

        # 给退出流程 1 秒；之后只强杀本次选择的服务树。
        self.after(1000, lambda files=selected_files: self._force_kill_files(files))

    def _force_kill_files(self, script_files):
        for file in script_files:
            forced = False
            targets = self.stopping_processes.pop(file, [])
            for target in targets:
                try:
                    if target.is_running():
                        target.kill()
                        forced = True
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

            proc = self.processes.get(file)
            if proc and proc.poll() is None:
                try:
                    proc.kill()
                    forced = True
                except OSError:
                    pass

            if forced:
                self.log("SYS", f"强制关闭 {file} 的残留进程")
            self.processes.pop(file, None)
            self.set_status(file, False)

    def stop_all(self):
        self.log("SYS", "正在停止所有服务...")
        self._stop_files([script["file"] for script in SCRIPTS])

    def force_kill_all(self):
        """兼容旧调用：强制清理全部受管服务。"""
        self._force_kill_files([script["file"] for script in SCRIPTS])

    def restart_all(self):
        self.stop_all()
        # 2秒后重启
        self.after(2000, self.start_all)

    def restart_web(self):
        self.log("SYS", "正在只重启 Web 服务...")
        self._stop_files(["start.py"])
        web_script = next(script for script in SCRIPTS if script["file"] == "start.py")
        self.after(2000, lambda: self.start_process(web_script))

    def clear_log(self):
        self.log_widget.configure(state='normal')
        self.log_widget.delete("1.0", "end")
        self.log_widget.configure(state='disabled')

    def on_closing(self):
        if getattr(self, "_closing", False):
            return
        self._closing = True
        if self.processes:
            self.stop_all()
            # 让上面的进程树清理与 1 秒兜底有机会完成。
            self.after(1200, self.destroy)
        else:
            self.destroy()

if __name__ == "__main__":
    app = ProcessManager()
    app.mainloop()
