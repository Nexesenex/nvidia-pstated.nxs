import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import subprocess
import sys
import os
import shlex


TOOLTIPS = {
    "ids": "GPU indices to manage (comma-separated, e.g. 0,1,2,3).\nLeave empty to manage ALL detected GPUs.\nIndices match what nvidia-smi shows.",
    "temperature_threshold": "If GPU temp exceeds this (in Celsius), the GPU is forced into\nlow P-State and fans turn on \u2014 regardless of utilization.\nDefault: 80 \u00b0C  |  Range: 0\u2013120",
    "utilization_threshold": "If GPU utilization is ABOVE this percent, switch to high P-State.\nDefault: 0% (meaning ANY utilization triggers high state).\nRange: 0\u2013100",
    "performance_state_low": "P-State (performance state) used when GPU is idle, cool, or\noverheated. Lower number = higher clocks / more power draw.\nDefault: 8  |  Range: 0 (max perf) \u2013 16 (min perf)",
    "performance_state_high": "P-State used when GPU is actively processing and cool.\nHigher number = lower clocks / less power draw.\nDefault: 16  |  Range: 0 (max perf) \u2013 16 (min perf)",
    "sleep_interval": "Delay (in milliseconds) between each monitoring loop.\nLower = more responsive but uses more CPU.\nDefault: 100 ms  |  Typical: 50\u20131000",
    "iterations_before_switch": "Debounce: how many consecutive low-utilization readings\nmust occur before switching from high P-State down to low.\nPrevents rapid back-and-forth toggling.\nDefault: 30 iterations",
    "iterations_before_idle": "How long all GPUs must remain in low P-State before the\ndisable-fan script is triggered.\nDefault: 9000 (\u224815 minutes at 100 ms sleep interval).",
    "iterations_before_keepalive": "How many iterations between executions of the keepalive\nfan script (to refresh fan-controller timeouts).\nDefault: 10 iterations",
    "disable_fan_script": "Shell command to run when external fans should turn OFF\n(e.g. curl a smart relay to cut AC power to rack fans).\nExecuted via system() \u2014 pipes, redirects, env vars work.\nDefault: none (no external fan control)",
    "enable_fan_script": "Shell command to run when external fans should turn ON.\nExecuted via system() \u2014 pipes, redirects, env vars work.\nDefault: none (no external fan control)",
    "keepalive_fan_script": "Shell command run periodically with FAN_STATE=1 (enabled)\nor FAN_STATE=2 (disabled) in the environment.\nUseful to keep smart relays or fan controllers alive.\nDefault: none",
    "binary_path": "Path to the nvidia-pstated executable.\nThe GUI looks for it in the current directory automatically.",
    "service_mode": "[Windows only] Pass --service to the binary, making it\nregister with the Service Control Manager (SCM).\nRequires administrator privileges to install the service.",
}


class ToolTip:
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tip_window = None
        self._id = None
        widget.bind("<Enter>", self.schedule, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")

    def schedule(self, event=None):
        self.widget.after_cancel(self._id) if self._id else None
        self._id = self.widget.after(400, self.show)

    def show(self, event=None):
        if self.tip_window:
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 5
        self.tip_window = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tw.wm_attributes("-topmost", True)
        label = tk.Label(
            tw,
            text=self.text,
            justify="left",
            background="#ffffe0",
            relief="solid",
            borderwidth=1,
            wraplength=400,
            font=("Segoe UI", 9),
            padx=6,
            pady=4,
        )
        label.pack()

    def hide(self, event=None):
        self.widget.after_cancel(self._id) if self._id else None
        self._id = None
        if self.tip_window:
            self.tip_window.destroy()
            self.tip_window = None


class PStateGUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("nvidia-pstated Control Panel")
        self.root.resizable(False, False)

        try:
            self.root.iconbitmap(default="")
        except Exception:
            pass

        style = ttk.Style()
        style.theme_use("vista" if "vista" in style.theme_names() else "clam")

        main = ttk.Frame(self.root, padding=12)
        main.pack(fill="both", expand=True)

        # --- Binary path row ---
        path_frame = ttk.LabelFrame(main, text="Executable", padding=8)
        path_frame.pack(fill="x", pady=(0, 8))

        self.binary_var = tk.StringVar(value=self._find_binary())
        path_entry = ttk.Entry(path_frame, textvariable=self.binary_var)
        path_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        ttk.Button(path_frame, text="Browse...", command=self._browse_binary).pack(side="right")
        self._add_tooltip(path_entry, TOOLTIPS["binary_path"])
        self._add_tooltip(
            ttk.Label(path_frame, text="?"),
            TOOLTIPS["binary_path"],
        )

        # --- Defaults (must be before _build_fields) ---
        self._defaults = {
            "ids": "",
            "temperature_threshold": "80",
            "utilization_threshold": "0",
            "performance_state_low": "8",
            "performance_state_high": "16",
            "sleep_interval": "100",
            "iterations_before_switch": "30",
            "iterations_before_idle": "9000",
            "iterations_before_keepalive": "10",
            "disable_fan_script": "",
            "enable_fan_script": "",
            "keepalive_fan_script": "",
        }

        nb = ttk.Notebook(main)
        nb.pack(fill="both", expand=True, pady=(0, 8))

        # --- Tab 1: GPU & Performance ---
        perf_frame = ttk.Frame(nb, padding=10)
        nb.add(perf_frame, text="GPU & Performance")

        fields = [
            ("GPU IDs (comma-separated):", "ids", 30),
            ("Temperature Threshold (°C):", "temperature_threshold", 5),
            ("Utilization Threshold (%):", "utilization_threshold", 5),
            ("Performance State (Low):", "performance_state_low", 5),
            ("Performance State (High):", "performance_state_high", 5),
        ]
        self._build_fields(perf_frame, fields)

        # --- Tab 2: Timing ---
        timing_frame = ttk.Frame(nb, padding=10)
        nb.add(timing_frame, text="Timing")

        fields = [
            ("Sleep Interval (ms):", "sleep_interval", 7),
            ("Iterations Before Switch:", "iterations_before_switch", 7),
            ("Iterations Before Idle:", "iterations_before_idle", 7),
            ("Iterations Before Keepalive:", "iterations_before_keepalive", 7),
        ]
        self._build_fields(timing_frame, fields)

        # --- Tab 3: Fan Scripts ---
        script_frame = ttk.Frame(nb, padding=10)
        nb.add(script_frame, text="Fan Scripts")

        fields = [
            ("Disable Fan Script:", "disable_fan_script", 60),
            ("Enable Fan Script:", "enable_fan_script", 60),
            ("Keepalive Fan Script:", "keepalive_fan_script", 60),
        ]
        self._build_fields(script_frame, fields)

        # --- Tab 4: Service ---
        svc_frame = ttk.Frame(nb, padding=10)
        nb.add(svc_frame, text="Service")

        self.service_var = tk.BooleanVar()
        cb = ttk.Checkbutton(
            svc_frame,
            text="Run as Windows Service (--service)",
            variable=self.service_var,
               )
        cb.pack(anchor="w", pady=4)
        self._add_tooltip(cb, TOOLTIPS["service_mode"])

        ttk.Label(
            svc_frame,
            text="Note: Service mode is only valid on Windows\nand usually requires administrator privileges.",
            foreground="gray",
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(0, 8))

        # --- Action buttons ---
        btn_frame = ttk.Frame(main)
        btn_frame.pack(fill="x", pady=(0, 6))

        ttk.Button(btn_frame, text="Generate Command", command=self._generate_cmd).pack(side="left", padx=(0, 4))
        ttk.Button(btn_frame, text="Copy Command", command=self._copy_cmd).pack(side="left", padx=(0, 4))
        ttk.Button(btn_frame, text="Launch Service", command=self._launch).pack(side="left", padx=(0, 4))
        ttk.Button(btn_frame, text="Reset Defaults", command=self._reset_defaults).pack(side="left")

        # --- Command preview ---
        preview_frame = ttk.LabelFrame(main, text="Command Preview", padding=6)
        preview_frame.pack(fill="x")

        self.cmd_text = tk.Text(preview_frame, height=4, wrap="word", font=("Consolas", 9))
        self.cmd_text.pack(fill="x")
        self.cmd_text.insert("1.0", "Configure options above, then click 'Generate Command'")
        self.cmd_text.config(state="disabled")

        # Center window
        self.root.update_idletasks()
        w = self.root.winfo_reqwidth() + 24
        h = self.root.winfo_reqheight() + 24
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = (sw - w) // 2
        y = (sh - h) // 2
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    # --------------- helpers ---------------
    def _find_binary(self):
        dir = os.path.dirname(os.path.abspath(__file__))
        for name in ("nvidia-pstated.exe", "nvidia-pstated"):
            path = os.path.join(dir, name)
            if os.path.isfile(path):
                return path
        return os.path.join(dir, "nvidia-pstated.exe")

    def _browse_binary(self):
        path = filedialog.askopenfilename(
            title="Select nvidia-pstated executable",
            filetypes=[("Executable", "*.exe"), ("All files", "*.*")],
            initialdir=os.path.dirname(self.binary_var.get()),
        )
        if path:
            self.binary_var.set(path)

    def _add_tooltip(self, widget, text):
        ToolTip(widget, text)

    def _build_fields(self, parent, fields):
        self._entries = getattr(self, "_entries", {})
        for i, (label, key, width) in enumerate(fields):
            row = ttk.Frame(parent)
            row.pack(fill="x", pady=3)
            lbl = ttk.Label(row, text=label, width=30, anchor="w")
            lbl.pack(side="left")
            var = tk.StringVar(value=self._defaults.get(key, ""))
            entry = ttk.Entry(row, textvariable=var, width=width)
            entry.pack(side="left", padx=(0, 4))
            tip_lbl = ttk.Label(row, text="?", foreground="#4a90d9", cursor="hand2")
            tip_lbl.pack(side="left")
            self._add_tooltip(entry, TOOLTIPS.get(key, ""))
            self._add_tooltip(tip_lbl, TOOLTIPS.get(key, ""))
            setattr(self, f"{key}_var", var)
            self._entries[key] = var

    def _get_args(self):
        args = []
        binary = self.binary_var.get().strip()
        if binary:
            args.append(binary)

        # service flag — must come early for Windows service detection
        ids = self._entries["ids"].get().strip()
        if ids:
            args.extend(["--ids", ids])
        tt = self._entries["temperature_threshold"].get().strip()
        if tt and tt != "80":
            args.extend(["--temperature-threshold", tt])
        ut = self._entries["utilization_threshold"].get().strip()
        if ut and ut != "0":
            args.extend(["--utilization-threshold", ut])
        psl = self._entries["performance_state_low"].get().strip()
        if psl and psl != "8":
            args.extend(["--performance-state-low", psl])
        psh = self._entries["performance_state_high"].get().strip()
        if psh and psh != "16":
            args.extend(["--performance-state-high", psh])
        si = self._entries["sleep_interval"].get().strip()
        if si and si != "100":
            args.extend(["--sleep-interval", si])
        ibs = self._entries["iterations_before_switch"].get().strip()
        if ibs and ibs != "30":
            args.extend(["--iterations-before-switch", ibs])
        ibi = self._entries["iterations_before_idle"].get().strip()
        if ibi and ibi != "9000":
            args.extend(["--iterations-before-idle", ibi])
        ibk = self._entries["iterations_before_keepalive"].get().strip()
        if ibk and ibk != "10":
            args.extend(["--iterations-before-keepalive", ibk])
        dfs = self._entries["disable_fan_script"].get().strip()
        if dfs:
            args.extend(["--disable-fan-script", dfs])
        efs = self._entries["enable_fan_script"].get().strip()
        if efs:
            args.extend(["--enable-fan-script", efs])
        kfs = self._entries["keepalive_fan_script"].get().strip()
        if kfs:
            args.extend(["--keepalive-fan-script", kfs])
        if self.service_var.get():
            args.append("--service")
        return args

    def _generate_cmd(self):
        args = self._get_args()
        if not args:
            self._set_preview("Select a binary path first.")
            return
        quoted = []
        for a in args:
            if " " in a or "&" in a or "|" in a:
                quoted.append(shlex.quote(a))
            else:
                quoted.append(a)
        self._set_preview(" ".join(quoted))

    def _copy_cmd(self):
        self._generate_cmd()
        text = self.cmd_text.get("1.0", "end-1c")
        if text and "Select a binary" not in text:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            messagebox.showinfo("Copied", "Command copied to clipboard.", parent=self.root)

    def _launch(self):
        args = self._get_args()
        if not args:
            messagebox.showerror("Error", "No binary selected.", parent=self.root)
            return
        binary = args[0]
        if not os.path.isfile(binary):
            messagebox.showerror("Error", f"Binary not found:\n{binary}", parent=self.root)
            return
        cmd_args = args[1:]
        try:
            subprocess.Popen([binary] + cmd_args, shell=False)
            messagebox.showinfo(
                "Launched",
                f"Process started.\nPID: check Task Manager.\n\nCommand:\n{binary} {' '.join(shlex.quote(a) if (' ' in a or '&' in a or '|' in a) else a for a in cmd_args)}",
                parent=self.root,
            )
        except Exception as e:
            messagebox.showerror("Launch Error", str(e), parent=self.root)

    def _reset_defaults(self):
        for key, val in self._defaults.items():
            if key in self._entries:
                self._entries[key].set(val)
        self.service_var.set(False)
        self._set_preview("Defaults restored. Generate a command to preview.")

    def _set_preview(self, text):
        self.cmd_text.config(state="normal")
        self.cmd_text.delete("1.0", "end")
        self.cmd_text.insert("1.0", text)
        self.cmd_text.config(state="disabled")

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    PStateGUI().run()
