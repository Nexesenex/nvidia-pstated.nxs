import ctypes
import json
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import subprocess
import sys
import os
import shlex


def _is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def _runas(cmd_args):
    ctypes.windll.shell32.ShellExecuteW(
        None, "runas", cmd_args[0], " ".join(cmd_args[1:]) if len(cmd_args) > 1 else "",
        None, 1,
    )


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
    "profile_name": "Name of the currently selected profile.\nProfiles store all option values for quick reload.",
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

        # --- Profile manager ---
        prof_frame = ttk.LabelFrame(main, text="Profiles", padding=8)
        prof_frame.pack(fill="x", pady=(0, 8))

        row = ttk.Frame(prof_frame)
        row.pack(fill="x")
        self._profile_combo = ttk.Combobox(row, width=30, state="readonly")
        self._profile_combo.pack(side="left", padx=(0, 6))
        self._profile_combo.bind("<<ComboboxSelected>>", self._on_profile_select)
        self._add_tooltip(self._profile_combo, TOOLTIPS["profile_name"])
        ttk.Button(row, text="New", command=self._new_profile, width=8).pack(side="left", padx=1)
        ttk.Button(row, text="Save", command=self._save_current, width=8).pack(side="left", padx=1)
        ttk.Button(row, text="Save As", command=self._save_as, width=8).pack(side="left", padx=1)
        ttk.Button(row, text="Rename", command=self._rename_profile, width=8).pack(side="left", padx=1)
        ttk.Button(row, text="Delete", command=self._delete_profile, width=8).pack(side="left", padx=1)
        self._refresh_profile_list()

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

        # Detected GPUs info box
        gpu_info_frame = ttk.LabelFrame(perf_frame, text="Detected GPUs", padding=6)
        gpu_info_frame.pack(fill="x", pady=(0, 8))

        gpu_info_row = ttk.Frame(gpu_info_frame)
        gpu_info_row.pack(fill="x")
        self.gpu_info_text = tk.Text(
            gpu_info_row, height=4, width=50, wrap="none", font=("Consolas", 9),
            state="disabled", relief="sunken", borderwidth=2,
        )
        self.gpu_info_text.pack(side="left", fill="x", expand=True, padx=(0, 6))
        refresh_btn = ttk.Button(gpu_info_row, text="Refresh", command=self._refresh_gpu_info)
        refresh_btn.pack(side="right", anchor="n")
        self._populate_gpu_info()

        fields = [
            ("GPU IDs (comma-separated):", "ids", 30),
            ("Temperature Threshold (\u00b0C):", "temperature_threshold", 5),
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
            text="Append --service flag (Windows service mode)",
            variable=self.service_var,
               )
        cb.pack(anchor="w", pady=4)
        self._add_tooltip(cb, TOOLTIPS["service_mode"])

        ttk.Label(
            svc_frame,
            text="Note: --service flag is only valid on Windows and requires\nadministrator privileges when installing the service.",
            foreground="gray",
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(0, 10))

        ttk.Separator(svc_frame, orient="horizontal").pack(fill="x", pady=(0, 8))

        ctl_frame = ttk.LabelFrame(svc_frame, text="Service Control (Windows SCM)", padding=8)
        ctl_frame.pack(fill="x")

        ttk.Label(
            ctl_frame,
            text="Manage the installed 'nvidia-pstated' Windows service:",
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(0, 8))

        btn_row = ttk.Frame(ctl_frame)
        btn_row.pack()
        ttk.Button(btn_row, text="Start Service", command=self._start_service, width=16).pack(side="left", padx=3)
        ttk.Button(btn_row, text="Stop Service", command=self._stop_service, width=16).pack(side="left", padx=3)
        ttk.Button(btn_row, text="Restart Service", command=self._restart_service, width=16).pack(side="left", padx=3)

        ttk.Label(
            ctl_frame,
            text="Install: sc.exe create nvidia-pstated start=auto binPath=\"...path...\\nvidia-pstated.exe --service\"\nUninstall: sc.exe delete nvidia-pstated",
            foreground="gray",
            font=("Consolas", 8),
            justify="left",
        ).pack(anchor="w", pady=(8, 0))

        # --- Action buttons ---
        btn_frame = ttk.Frame(main)
        btn_frame.pack(fill="x", pady=(0, 4))

        ttk.Button(btn_frame, text="Generate Command", command=self._generate_cmd).pack(side="left", padx=(0, 4))
        ttk.Button(btn_frame, text="Copy Command", command=self._copy_cmd).pack(side="left", padx=(0, 4))
        ttk.Button(btn_frame, text="Reset Defaults", command=self._reset_defaults).pack(side="left")

        # --- Service control buttons (2nd row) ---
        svc_btn_frame = ttk.Frame(main)
        svc_btn_frame.pack(fill="x", pady=(0, 6))

        ttk.Label(svc_btn_frame, text="Service:", font=("Segoe UI", 9, "bold")).pack(side="left", padx=(0, 6))
        ttk.Button(svc_btn_frame, text="Start Service", command=self._start_service).pack(side="left", padx=(0, 4))
        ttk.Button(svc_btn_frame, text="Stop Service", command=self._stop_service).pack(side="left", padx=(0, 4))
        ttk.Button(svc_btn_frame, text="Restart Service", command=self._restart_service).pack(side="left", padx=(0, 4))
        ttk.Button(svc_btn_frame, text="Launch Process", command=self._launch).pack(side="left", padx=(0, 4))
        ttk.Label(
            svc_btn_frame,
            text="(start/stop/restart manage the Windows service via SCM)",
            foreground="gray",
            font=("Segoe UI", 8),
        ).pack(side="left", padx=(4, 0))

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

    def _query_gpus(self):
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=index,name", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=10, check=True,
            )
            lines = [line.strip() for line in result.stdout.strip().splitlines() if line.strip()]
            return lines
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return []

    def _populate_gpu_info(self):
        self.gpu_info_text.config(state="normal")
        self.gpu_info_text.delete("1.0", "end")
        gpus = self._query_gpus()
        if gpus:
            for line in gpus:
                self.gpu_info_text.insert("end", line + "\n")
        else:
            self.gpu_info_text.insert(
                "end",
                "No GPUs detected or nvidia-smi not found.\n"
                "Enter GPU IDs manually or leave empty to manage all.",
            )
        self.gpu_info_text.config(state="disabled")

    def _refresh_gpu_info(self):
        self._populate_gpu_info()

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

    def _run_sc(self, action):
        name = "nvidia-pstated"
        cmds = [["net", action, name], ["sc", action, name]]
        last_err = ""
        for cmd in cmds:
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            except FileNotFoundError:
                continue
            if result.returncode == 0:
                messagebox.showinfo(
                    f"Service {action.title()}",
                    f"Service '{name}' {action}ed successfully.",
                    parent=self.root,
                )
                return
            last_err = result.stderr.strip() or result.stdout.strip()
            if "access is denied" in last_err.lower():
                break
        if "access is denied" in last_err.lower():
            if not _is_admin():
                reply = messagebox.askyesno(
                    "Access Denied",
                    "Service management requires administrator privileges.\n\n"
                    "Would you like to retry with elevated permissions?",
                    parent=self.root,
                )
                if reply:
                    if action == "restart":
                        _runas(["net", "stop", name])
                        _runas(["net", "start", name])
                    else:
                        _runas(["net", action, name])
                    messagebox.showinfo(
                        "Elevation Requested",
                        f"An elevated window should appear.\n"
                        f"Complete the UAC prompt to {action} the service.",
                        parent=self.root,
                    )
            else:
                messagebox.showerror(
                    f"Service {action.title()}",
                    f"Failed to {action} service '{name}':\n{last_err}",
                    parent=self.root,
                )
        else:
            messagebox.showerror(
                f"Service {action.title()}",
                f"Failed to {action} service '{name}':\n{last_err}",
                parent=self.root,
            )

    def _start_service(self):
        self._run_sc("start")

    def _stop_service(self):
        self._run_sc("stop")

    def _restart_service(self):
        name = "nvidia-pstated"
        cmds = [["net", "stop", name], ["net", "start", name]]
        failures = []
        for cmd in cmds:
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                if result.returncode != 0:
                    failures.append((cmd, result.stderr.strip() or result.stdout.strip()))
            except FileNotFoundError:
                failures.append((cmd, "command not found"))
                break
            except subprocess.TimeoutExpired:
                failures.append((cmd, "timed out"))
        if not failures:
            messagebox.showinfo("Service Restart", "Service 'nvidia-pstated' restarted successfully.", parent=self.root)
        else:
            last_err = failures[0][1]
            if "access is denied" in last_err.lower():
                if not _is_admin():
                    reply = messagebox.askyesno(
                        "Access Denied",
                        "Service management requires administrator privileges.\n\n"
                        "Would you like to retry with elevated permissions?",
                        parent=self.root,
                    )
                    if reply:
                        _runas(["net", "stop", name])
                        _runas(["net", "start", name])
                        messagebox.showinfo(
                            "Elevation Requested",
                            "An elevated window should appear.\n"
                            "Complete the UAC prompt to restart the service.",
                            parent=self.root,
                        )
                else:
                    messagebox.showerror("Service Restart", f"Failed to restart service:\n{last_err}", parent=self.root)
            else:
                messagebox.showerror("Service Restart", f"Failed to restart service:\n{last_err}", parent=self.root)

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

    # --------------- profile manager ---------------
    def _profile_dir(self):
        d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "profiles")
        os.makedirs(d, exist_ok=True)
        return d

    def _list_profiles(self):
        ext = ".json"
        dir_ = self._profile_dir()
        names = []
        for f in sorted(os.listdir(dir_)):
            if f.endswith(ext):
                names.append(f[: -len(ext)])
        return names

    def _profile_path(self, name):
        return os.path.join(self._profile_dir(), name + ".json")

    def _profile_data(self):
        data = {}
        for key in self._defaults:
            data[key] = self._entries[key].get()
        data["binary_path"] = self.binary_var.get()
        data["service_mode"] = self.service_var.get()
        return data

    def _apply_profile(self, data):
        for key in self._defaults:
            if key in data:
                self._entries[key].set(data[key])
        if "binary_path" in data:
            self.binary_var.set(data["binary_path"])
        if "service_mode" in data:
            self.service_var.set(data["service_mode"] == True)

    def _refresh_profile_list(self):
        self._profile_combo["values"] = self._list_profiles()
        current = self._profile_combo.get()
        if current and current in self._list_profiles():
            self._profile_combo.set(current)
        else:
            self._profile_combo.set("")

    def _on_profile_select(self, event=None):
        name = self._profile_combo.get()
        if not name:
            return
        path = self._profile_path(name)
        if not os.path.isfile(path):
            return
        try:
            with open(path, "r") as f:
                data = json.load(f)
            self._apply_profile(data)
        except Exception as e:
            messagebox.showerror("Load Error", f"Failed to load profile:\n{e}", parent=self.root)

    def _save_profile(self, name, data):
        path = self._profile_path(name)
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        self._refresh_profile_list()
        self._profile_combo.set(name)

    def _new_profile(self):
        name = self._ask_name("New Profile", "Enter a name for the new profile:")
        if not name:
            return
        if os.path.isfile(self._profile_path(name)):
            messagebox.showerror("Error", f"Profile '{name}' already exists.", parent=self.root)
            return
        self._save_profile(name, self._profile_data())

    def _save_current(self):
        name = self._profile_combo.get()
        if not name:
            self._save_as()
            return
        self._save_profile(name, self._profile_data())

    def _save_as(self):
        name = self._ask_name("Save As", "Enter a new profile name:")
        if not name:
            return
        self._save_profile(name, self._profile_data())

    def _rename_profile(self):
        old = self._profile_combo.get()
        if not old:
            messagebox.showinfo("Rename", "Select a profile first.", parent=self.root)
            return
        new = self._ask_name("Rename Profile", "Enter the new name:", initial=old)
        if not new or new == old:
            return
        old_path = self._profile_path(old)
        new_path = self._profile_path(new)
        if os.path.isfile(new_path):
            messagebox.showerror("Error", f"Profile '{new}' already exists.", parent=self.root)
            return
        try:
            os.rename(old_path, new_path)
            self._refresh_profile_list()
            self._profile_combo.set(new)
        except OSError as e:
            messagebox.showerror("Rename Error", str(e), parent=self.root)

    def _delete_profile(self):
        name = self._profile_combo.get()
        if not name:
            messagebox.showinfo("Delete", "Select a profile first.", parent=self.root)
            return
        if not messagebox.askyesno("Confirm Delete", f"Delete profile '{name}'?", parent=self.root):
            return
        path = self._profile_path(name)
        try:
            os.remove(path)
            self._refresh_profile_list()
        except OSError as e:
            messagebox.showerror("Delete Error", str(e), parent=self.root)

    def _ask_name(self, title, prompt, initial=""):
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()
        f = ttk.Frame(dialog, padding=12)
        f.pack()
        ttk.Label(f, text=prompt).pack(anchor="w")
        var = tk.StringVar(value=initial)
        entry = ttk.Entry(f, textvariable=var, width=40)
        entry.pack(pady=6)
        entry.select_range(0, "end")
        entry.focus()
        result = [None]
        def ok():
            result[0] = var.get().strip()
            dialog.destroy()
        def cancel():
            dialog.destroy()
        bf = ttk.Frame(f)
        bf.pack()
        ttk.Button(bf, text="OK", command=ok).pack(side="left", padx=4)
        ttk.Button(bf, text="Cancel", command=cancel).pack(side="left", padx=4)
        dialog.bind("<Return>", lambda e: ok())
        dialog.bind("<Escape>", lambda e: cancel())
        self.root.wait_window(dialog)
        return result[0]

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    PStateGUI().run()
