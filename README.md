# Engent

> ## 📦 ARŞİV — devre dışı (2026-10-11)
> Proje artık kullanılmıyor ama silinmedi. Bu klasör olduğu gibi saklanır,
> aşağıdaki **Yeniden ayağa kaldırma** adımlarıyla tekrar çalışır.

## Yeniden ayağa kaldırma

**Klasörde ne var**

| Duruyor | Ne |
|---|---|
| `engent/` | tüm kaynak kod |
| `install.ps1` · `unlock.cmd` · `requirements.txt` | kurulum, kilit açma, bağımlılık listesi |
| `.env.example` · `projects.example.json` | ayar şablonları |
| `.env` · `projects.json` | **senin son ayarların** (Telegram/Claude sırları içerir, paylaşma/commit'leme) |
| `data/` | görev geçmişi (`engent.db`), log, `model.json`, `autonomy.txt`, `limits.json` |

**Silindi (kurulumla geri gelir):** `.venv` (~314 MB: `claude-agent-sdk` + içindeki `claude.exe`, `pywinpty`, `httpx`) ve `__pycache__`.
Kurulum için başka hiçbir şey indirilmez: Python 3.10+ ve Git for Windows makinede olmalı.

**Adımlar** (Windows; ayrıntılar aşağıda "Kurulum"da):

1. `python -m venv .venv` ve `.venv\Scripts\python -m pip install -r requirements.txt`
2. Claude token'ı süresi dolmuş olabilir: `.venv\Lib\site-packages\claude_agent_sdk\_bundled\claude.exe setup-token` → çıkan değeri `.env`'deki `CLAUDE_CODE_OAUTH_TOKEN`'a yaz.
3. `projects.json`'daki `path` değerlerini yeni makineye göre düzelt (yoksa `projects.example.json`'dan başla).
4. Ön planda dene: `.venv\Scripts\python -m engent` → telefona "🟢 Engent çevrimiçi" gelmeli.
5. Servis olarak kur (yönetici PowerShell): `.\install.ps1` (eski görev varsa üzerine yazar).

**Dikkat**

- **Aynı bot token'ı iki makinede aynı anda çalışamaz** (Telegram `409 Conflict`; mesajları biri çalar). Debian sunucu aynı botu kullanıyorsa Engent'i açmadan önce orayı kapat ya da [@BotFather](https://t.me/BotFather) → `/revoke` ile yeni token al.
- Kilitli açılır: `data/seen` eski olduğundan bot ilk mesajda şifre ister (`BOT_PASSWORD`).
- Kod Windows'a yazılı (`pywinpty`, Task Scheduler/S4U, `.cmd`/`.ps1`, `msvcrt` kilidi). Linux'a taşımak ayrı bir iştir.
- `%USERPROFILE%\engent-worktrees\` bu klasörün dışındadır; eski görevlerin worktree'leri ve `engent/<id>` dalları orada/proje repolarında kalmış olabilir. Gerekirse `git worktree prune` ve `git branch -D engent/<id>` ile temizle.
- Arşivlenirken Task Scheduler'daki `Engent` görevi hâlâ kayıtlıydı (silmek yönetici yetkisi ister; `.venv` olmadığı için başlayamaz). Yönetici PowerShell'de `.\install.ps1 -Uninstall` ile kaldırılır.

---


Windows makineni 7/24 çalışan bir kodlama işçisine, telefonunu da komuta merkezine çevirir.
Telefondan görev yazarsın, ajan izole bir git dalında çalışır. Telefon yalnızca karar anlarında titrer
(komut onayı, diff onayı, ajanın sorusu). Testler geçip deploy bitince önizleme linki ve **Merge** butonu gelir.

```
 Telefon (Telegram)                         Windows (Task Scheduler, S4U, yönetici yetkisi yok)
 ───────────────────                        ─────────────────────────────────────────────────────
  görev / onay / yanıt   ── HTTPS ──►  Telegram  ◄── long-poll (yalnız giden bağlantı) ── engent daemon
                                                                                          │
   🔔 onay: diff/komut   ◄────────────────────────────────────────────── tg.ask() ◄──┐    │ SQLite durum makinesi
   🔕 ilerleme (sessiz)                                                               │    ▼
   ✅ link + [Merge]                                              PreToolUse hook ─ policy (deny/allow/edit/ask)
                                                                  can_use_tool ────┘    │
                                                        Claude Agent SDK (claude.exe) ─ git worktree: engent/<id>
                                                        veya ConPTY (aider vb.)          │
                                                                   test → düzelt (≤N) → commit → deploy → rapor
```

## Kurulum

1. **Telegram botu:** [@BotFather](https://t.me/BotFather) → `/newbot` → token. Kendi kullanıcı id'ni [@userinfobot](https://t.me/userinfobot)'tan al.
2. **Bağımlılıklar** (Python 3.10+, Git for Windows):
   ```powershell
   python -m venv .venv
   .venv\Scripts\python -m pip install -r requirements.txt
   ```
3. **Claude kimliği:** abonelik için uzun ömürlü token üret, `.env`'deki `CLAUDE_CODE_OAUTH_TOKEN`'a koy (ya da `ANTHROPIC_API_KEY` kullan):
   ```powershell
   .venv\Lib\site-packages\claude_agent_sdk\_bundled\claude.exe setup-token
   ```
4. `.env.example` → `.env`, `projects.example.json` → `projects.json` kopyala ve doldur.
5. **Önce ön planda dene:** `.venv\Scripts\python -m engent` → telefona "🟢 Engent çevrimiçi" gelmeli. Bota bir görev yaz.
6. **Servis olarak kur** (yönetici PowerShell): `.\install.ps1` — kaldırmak için `.\install.ps1 -Uninstall`.
7. **Uykuyu kapat** (prizdeyken): `powercfg /change standby-timeout-ac 0`

## Telefondan kullanım

| Mesaj | Ne olur |
|---|---|
| düz metin | varsayılan projede (projects.json'daki ilk proje) yeni görev; o projede son 24 saatte devam ettirilebilecek görev varsa önce sorar: **↩️ #N'e devam** / **🆕 Yeni görev** |
| `/new web login sayfası ekle` | `web` projesinde görev |
| `/status` · `/log 12` · `/stop 12` · `/projects` | durum (çalışan ajanın son adımıyla), görev logunun sonu, durdurma (`/stop` tek başına: çalışanlar butonla; çalışmayan görevde durumunu + ▶️ Devam et söyler), proje listesi (⭐ varsayılan) |
| (otomatik) | çalışan görevin ▶️ mesajı sessizce yerinde güncellenir: durum, süre, adım sayısı, ajanın son cümlesi, son 5 adım (📖 okuma, ✏️ düzenleme, 🖥 komut), 2+ dk hareketsizse uyarı, **⏹ Durdur** butonu |
| `/use 3d` | varsayılan projeyi değiştirir (projects.json'da ilk sıraya alır) |
| `/add 3d C:\Users\kayas\Desktop\3d-main` | klasörü proje ekler (yol yoksa bulunduğun klasör); git reposu değilse `.gitignore` + `git init` + ilk commit |
| `/ls` · `/cd Desktop` · `/cd ..` | klasörlerde gez; listedeki butonlarla gir/çık, **⭐ Bu klasörle çalış** = ekle + varsayılan yap (git'siz klasörde önce onay sorar) |
| `/run deploy.cmd` · `/run npm run build` | bulunduğun klasörde (`/cd`) exe/cmd/bat/ps1 ya da komut çalıştırır: önce ▶️ onay butonu, sonra çıktının sonu gelir. Pencere açılmaz (servis arka planda çalışır), girdi beklemez (`pause` geçilir), `CMD_TIMEOUT_MIN` sonra durdurulur |
| `/usage` · `/compact 12` | plan limiti + görev başına bağlam/maliyet; görevin bağlamını özetleyip küçültme |
| `/izin` | onay seviyesi butonla: 🔒 her şeyi sor · ✏️ düzenlemeler otomatik · 🚀 tam otonom (guardrail yasakları her seviyede geçerli; çalışan ajanlara da hemen uygulanır) |
| `/help` | tüm komutlar (Telegram'da `/` menüsünde de listelenir) |
| butona dokun | onayla / reddet / "bu görevde tüm düzenlemeler" / soru seçeneği |
| onay mesajını **yanıtla** | reddet + ajana gerekçe/yön ver ("lodash değil native kullan") |
| bir görevin herhangi bir mesajını **yanıtla** | sormadan aynı dal + aynı ajan oturumunda ek iterasyon (yanıt `/` ile başlıyorsa komut olarak çalışır) |

## projects.json alanları

| Alan | Açıklama |
|---|---|
| `path` | yerel git reposu (zorunlu) |
| `base` | dal, varsayılan `main`; worktree buradan açılır, merge buraya yapılır |
| `engine` | `claude` (varsayılan, yapılandırılmış onay) ya da `pty` (herhangi bir etkileşimli CLI) |
| `cmd` | yalnız `pty` için: argv listesi, `{task}` görev metniyle değişir |
| `setup` | worktree açılınca bir kez (ör. `npm ci`) |
| `test` | ajan bitince koşar; kırmızıysa çıktı ajana geri beslenir (`MAX_FIX_ATTEMPTS` kez) |
| `deploy` | testler geçince koşar; çıktıdaki son `https://…` link olarak gelir (`url_regex` ile değiştir) |
| `deploy_env` | `.env`'den yalnız bu deploy komutuna geçecek sır adları |
| `auto_edits` | `true`: worktree içindeki dosya düzenlemeleri onaysız (izole dal + merge onayı yeterli sayılırsa) |
| `auto_allow` | ek regex'ler: eşleşen kabuk komutları onaysız çalışır |
| `push_on_merge` | merge sonrası `git push origin <base>` |

## Güvenlik modeli

- **Uzak erişim:** gelen port yok, tünel yok. Daemon Telegram'a yalnız giden HTTPS ile bağlanır; `TELEGRAM_ALLOWED_USERS` dışındaki herkes yok sayılır.
- **Şifre kilidi:** `.env`'de `BOT_PASSWORD` doluysa `LOCK_AFTER_MIN` (60) dk mesaj/buton olmayınca bot önce şifreyi sorar; kilitliyken mesaj, komut ve onay butonları işlemez (bekleyen onaylar beklemede kalır). Son etkinlik `data/seen` dosyasının zamanı: restart kilidi açmaz. Şifre denemeleri sohbetten silinir.
- **5 yanlış şifre:** kilitliyken gelen her mesaj bir deneme sayılır (`data/fails`, restart sıfırlamaz, yalnız doğru şifre sıfırlar). 5.'sinde bot son bir mesajla `unlock.cmd`'yi söyler ve susar: hiçbir mesaj/butona cevap vermez, bildirim göndermez, yeni onay istekleri anında reddedilir (görev durur). Yeniden açmak için bilgisayarda `unlock.cmd` çalıştırılır (`data/lockout`'u siler; restart gerekmez). Ajanlar bu dosyalara dokunamaz (guardrail).
- **İzolasyon:** her görev `%USERPROFILE%\engent-worktrees\` altında ayrı worktree + `engent/<id>` dalı. Ana çalışma kopyasına yalnız sen **Merge**'e basınca dokunulur.
- **Guardrail'ler** (`engent/policy.py`, `python -m engent.policy` ile öz-test): disk/kayıt defteri/servis/güvenlik ayarı komutları, kök/ev dizininde yıkıcı komutlar, `curl | sh`, `git push`/dal silme, deploy/publish, kimlik dosyaları ve Engent'in kendi dosyaları **hiç çalışmaz** (onay bile sorulmaz). Salt okunur ve test komutları sessiz geçer; geri kalan her şey telefona gelir. Worktree dışına yazma reddedilir.
- **Sırlar:** `.env` ortam değişkenlerine aktarılmaz. Ajan yalnız `AGENT_ENV_KEYS`'i, deploy yalnız `deploy_env`'i görür.
- **Kaçak döngü:** `MAX_TURNS`, `MAX_BUDGET_USD`, `TASK_TIMEOUT_MIN`, `MAX_FIX_ATTEMPTS`; cevapsız onay `APPROVAL_TIMEOUT_MIN` sonra reddedilir (fail-closed).
- **Dayanıklılık:** durum SQLite'ta; yeniden başlatmada yarım kalan görevler "▶️ Devam et" butonuyla aynı ajan oturumundan sürer. Süreç kendini yeniden başlatır; Task Scheduler 5 dakikada bir bekçilik yapar; tek-örnek kilidi çift çalışmayı engeller.

## Bilinen sınırlar

- Ajan senin kullanıcınla çalışır: guardrail'ler en iyi çaba, sınır değil. Tam izolasyon için ayrı bir Windows kullanıcısı ya da WSL2/Docker gerekir.
- Diff ve komutlar Telegram sunucularından geçer (bot sohbetleri uçtan uca şifreli değil).
- `pty` motoru komutları çalıştırılmadan önce göremez; yalnız CLI'ın kendi sorduğu soruları telefona taşır.
- Merge, ana repo `base` dalındayken ve temizken yapılır; değilse telefona sebep yazılır.
