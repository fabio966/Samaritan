import customtkinter as ctk
import subprocess
import threading
import sys

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

class BotLauncher(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Pulichimica Bot - Dashboard Operativa")
        self.geometry("900x550")
        
        self.bot_process = None

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.sidebar = ctk.CTkFrame(self, width=220, corner_radius=0)
        self.sidebar.grid(row=0, column=0, sticky="nsew")

        self.logo_label = ctk.CTkLabel(self.sidebar, text="PULICHIMICA", font=ctk.CTkFont(size=22, weight="bold"))
        self.logo_label.grid(row=0, column=0, padx=20, pady=(20, 30))

        self.btn_start = ctk.CTkButton(self.sidebar, text="▶️ Avvia Bot", fg_color="#28a745", hover_color="#218838", command=self.start_bot)
        self.btn_start.grid(row=1, column=0, padx=20, pady=10)

        self.btn_stop = ctk.CTkButton(self.sidebar, text="⏹️ Ferma Bot", fg_color="#dc3545", hover_color="#c82333", state="disabled", command=self.stop_bot)
        self.btn_stop.grid(row=2, column=0, padx=20, pady=10)

        self.div = ctk.CTkFrame(self.sidebar, height=2, fg_color="gray30")
        self.div.grid(row=3, column=0, padx=20, pady=20, sticky="ew")

        self.btn_update = ctk.CTkButton(self.sidebar, text="🔄 Forza Aggiornamento", command=self.cmd_update)
        self.btn_update.grid(row=4, column=0, padx=20, pady=10)

        self.btn_stock = ctk.CTkButton(self.sidebar, text="📦 Check Scorte Ora", command=self.cmd_stock)
        self.btn_stock.grid(row=5, column=0, padx=20, pady=10)

        self.btn_report = ctk.CTkButton(self.sidebar, text="📊 Invia Report Drive", command=self.cmd_report)
        self.btn_report.grid(row=6, column=0, padx=20, pady=10)

        self.log_box = ctk.CTkTextbox(self, width=650, corner_radius=10, font=ctk.CTkFont(family="Consolas", size=13))
        self.log_box.grid(row=0, column=1, padx=20, pady=20, sticky="nsew")
        self.log_box.insert("0.0", "--- Dashboard Inizializzata ---\nIn attesa di istruzioni...\n\n")
        self.log_box.configure(state="disabled")

    def scrivi_log(self, testo):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", testo)
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def leggi_output_bot(self):
        while self.bot_process is not None:
            try:
                riga = self.bot_process.stdout.readline()
                if not riga:
                    break
                self.scrivi_log(riga)
            except Exception:
                break

    def start_bot(self):
        if self.bot_process is None:
            self.scrivi_log(">>> AVVIO DEL MOTORE PULICHIMICA IN BACKGROUND...\n")
            
            startupinfo = None
            if sys.platform == "win32":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            self.bot_process = subprocess.Popen(
                ["python", "-u", "main.py"], 
                stdout=subprocess.PIPE, 
                stderr=subprocess.STDOUT, 
                text=True,
                bufsize=1,
                startupinfo=startupinfo
            )
            
            threading.Thread(target=self.leggi_output_bot, daemon=True).start()
            
            self.btn_start.configure(state="disabled")
            self.btn_stop.configure(state="normal")

    def stop_bot(self):
        if self.bot_process is not None:
            self.scrivi_log("\n>>> ARRESTO FORZATO DEL BOT...\n")
            self.bot_process.terminate()
            self.bot_process = None
            
            self.btn_start.configure(state="normal")
            self.btn_stop.configure(state="disabled")
            self.scrivi_log(">>> Bot disattivato correttamente.\n\n")

    def cmd_update(self):
        self.scrivi_log(">>> [GUI] Richiesto Aggiornamento OTA...\n")
        open("update.flag", "w").close()

    def cmd_stock(self):
        self.scrivi_log(">>> [GUI] Richiesto Check Scorte Immediato...\n")
        open("stock.flag", "w").close()

    def cmd_report(self):
        self.scrivi_log(">>> [GUI] Richiesto Invio Report Drive...\n")
        open("report.flag", "w").close()

if __name__ == "__main__":
    app = BotLauncher()
    app.mainloop()
