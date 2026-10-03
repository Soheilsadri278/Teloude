# Teloude

**از فایل‌های خودت مستقیماً روی حساب تلگرام خودت نسخه پشتیبان بگیر.**

Teloude یک برنامه دسکتاپ ویندوزی برای پشتیبان‌گیری از فایل‌هاست که از **حساب شخصی Telegram شما** به‌عنوان فضای ذخیره‌سازی خصوصی و ابری استفاده می‌کند.

Teloude برای **Backup** طراحی شده، نه Sync.
این برنامه به‌صورت خودکار فایل‌های محلی شما را حذف نمی‌کند.

[🇬🇧 English](README.md)

---

## ✨ ویژگی‌ها

### 💾 ذخیره‌سازی

* استفاده از حساب شخصی Telegram
* بدون نیاز به سرور اختصاصی
* بدون اشتراک ماهانه
* بدون سرویس ابری شخص ثالث
* استفاده از Telegram MTProto از طریق Telethon
* استفاده از Private Forum Supergroup به‌عنوان فضای ذخیره‌سازی
* تبدیل پوشه‌های محلی به Topicهای Telegram
* پشتیبانی از ساختار پوشه‌ها و زیرپوشه‌ها
* ذخیره اطلاعات ساختار فایل‌ها در دیتابیس محلی SQLite
* ذخیره اطلاعات لازم داخل Caption پیام‌ها برای امکان بازیابی و شناسایی فایل‌ها

### 📦 پشتیبان‌گیری

* اسکن فایل‌ها و پوشه‌ها
* تشخیص فایل‌های تکراری
* انتخاب نحوه برخورد با فایل‌های تکراری:

  * Skip
  * Upload again
  * Cancel
  * Apply to all
* آپلود چندبخشی و Pipeline
* پشتیبانی از Resume
* Pause / Resume
* Cancel
* Retry
* Reconnect
* ادامه عملیات پس از Shutdown یا Crash
* جلوگیری از Path Traversal هنگام Restore
* امکان پیدا کردن فایل‌ها روی Telegram و بازسازی Index محلی

### 🔄 بازیابی

هنگام Restore می‌توان برای فایل‌های موجود تصمیم گرفت:

* Skip
* Keep both
* Replace
* Cancel

Teloude هنگام بازیابی مسیر فایل‌ها را بررسی می‌کند تا از خروج فایل از مسیر مقصد جلوگیری شود.

### ⚡ کنترل سرعت

دارای Speed Limiter داخلی با Token Bucket غیرهمزمان.

سرعت آپلود را می‌توان روی گزینه‌های زیر تنظیم کرد:

* Unlimited
* 10 MB/s
* 5 MB/s
* 2 MB/s
* Custom

محدودکننده سرعت به‌صورت سراسری روی عملیات انتقال اعمال می‌شود و باعث از بین رفتن همزمانی Pipeline نمی‌شود.

### 🌐 Proxy

* پشتیبانی از MTProto Proxy
* تنظیم Proxy به‌صورت Global
* امکان مدیریت تنظیمات Proxy از داخل برنامه

### 🖥️ رابط کاربری

* Windows Desktop UI
* ساخته‌شده با PySide6
* System Tray
* Notification
* پشتیبانی از Theme
* Single Instance
* امکان انتقال برنامه به Tray هنگام بستن پنجره
* رابط کاربری ساده و متمرکز بر عملیات Backup

### 🔐 امنیت

* ذخیره امن Credentialهای محلی با Windows DPAPI
* عدم قرار دادن Secretها در Logها
* جلوگیری از Overwrite بدون تصمیم کاربر
* عدم حذف خودکار فایل‌های محلی
* عدم حذف مخفیانه فایل‌های ذخیره‌شده روی Telegram
* جلوگیری از گزارش موفقیت جعلی

---

## 🛡️ قوانین اصلی Teloude

| قانون                     | رفتار Teloude                                          |
| ------------------------- | ------------------------------------------------------ |
| Backup، نه Sync           | فایل‌های محلی به‌صورت خودکار حذف نمی‌شوند              |
| بدون حذف خودکار           | Teloude فایل‌های شما را برای آزاد کردن فضا پاک نمی‌کند |
| بدون Overwrite مخفیانه    | جایگزینی فایل نیاز به تصمیم مشخص دارد                  |
| بدون Cloud Delete مخفیانه | فایل Telegram بدون تصمیم کاربر حذف نمی‌شود             |
| بدون Secret در Log        | اطلاعات حساس در Log ذخیره نمی‌شوند                     |
| بدون Fake Success         | عملیات ناموفق به‌عنوان موفق گزارش نمی‌شود              |

---

## 🏗️ ساختار پروژه

```text
Teloude/
├── teloude/
│   ├── app/
│   │   ├── domain/
│   │   ├── application/
│   │   ├── infrastructure/
│   │   └── presentation/
│   ├── tests/
│   ├── scripts/
│   ├── installer/
│   └── docs/
│
├── Logo&icon/
├── README.md
├── README.fa.md
└── ...
```

---

## 🚀 نصب و اجرا

### پیش‌نیازها

* Windows
* Python 3.11+
* Telegram Account
* Telegram API ID
* Telegram API Hash

وابستگی‌های اصلی:

* PySide6-Essentials
* Telethon
* Pillow
* pypdf

### نصب

ابتدا Repository را دریافت کنید:

```bash
git clone https://github.com/Soheilsadri278/Teloude.git
cd Teloude/teloude
```

سپس Virtual Environment بسازید:

```bash
python -m venv .venv
```

فعال‌سازی در PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

نصب وابستگی‌ها:

```powershell
pip install -e ".[dev]"
```

### تنظیم Telegram API

می‌توانید API ID و API Hash را از طریق Environment Variable تنظیم کنید:

```powershell
$env:TELOUDE_API_ID="YOUR_API_ID"
$env:TELOUDE_API_HASH="YOUR_API_HASH"
```

سپس برنامه را اجرا کنید:

```powershell
python -m app
```

در اولین اجرا، Teloude از شما اطلاعات لازم برای ورود به حساب Telegram را دریافت می‌کند.

---

## 🔑 ورود به Telegram

در اولین اجرا:

1. شماره تلفن Telegram را وارد کنید.
2. کد ورود Telegram را وارد کنید.
3. در صورت فعال بودن Two-Step Verification، رمز عبور را وارد کنید.

پس از ورود، Session به‌صورت محلی ذخیره می‌شود.

---

## 📁 محل ذخیره اطلاعات

اطلاعات محلی برنامه به‌صورت پیش‌فرض در مسیر زیر قرار می‌گیرند:

```text
%LOCALAPPDATA%\Teloude
```

Logها:

```text
%LOCALAPPDATA%\Teloude\logs\teloude.log
```

---

## 🧰 Command Line Options

Teloude از گزینه‌های زیر پشتیبانی می‌کند:

```text
--version
--data-dir PATH
--screenshot FILE
--open-proxy
```

مثلاً:

```powershell
python -m app --version
```

---

## 📦 Build کردن نسخه Windows

برای Build نسخه Windows:

```powershell
powershell -File scripts\build_windows.ps1
```

برای ساخت نسخه Portable:

```powershell
powershell -File scripts\build_portable.ps1
```

برای ساخت Portable به‌صورت ZIP:

```powershell
powershell -File scripts\build_portable.ps1 -Zip
```

---

## 🔐 API Credentials

Teloude از چند روش برای دریافت API Credentials پشتیبانی می‌کند:

### 1. Environment Variables

```text
TELOUDE_API_ID
TELOUDE_API_HASH
```

### 2. Build-time Configuration

فایل اختیاری:

```text
app/teloude_api.json
```

این فایل در Git قرار نمی‌گیرد.

### 3. ورود توسط کاربر

کاربر می‌تواند اطلاعات موردنیاز را هنگام استفاده از برنامه وارد کند.

اطلاعات حساس به‌صورت محلی و با استفاده از Windows DPAPI محافظت می‌شوند.

---

## 🔄 قابل حمل بودن

فایل‌های برنامه و Assetها قابلیت انتقال دارند.

اما Session حساب Telegram و Credentialهای API عمداً قابل انتقال مستقیم نیستند، زیرا اطلاعات حساس با Windows DPAPI و کاربر/سیستم مربوطه محافظت می‌شوند.

نسخه Portable با Data خالی ارائه می‌شود.

---

## 🧪 تست و بررسی

اجرای تست‌ها:

```bash
python -m pytest -q
```

بررسی کد با Ruff:

```bash
python -m ruff check .
```

اجرای Benchmark:

```bash
python scripts/benchmarks.py
```

---

## 🎨 آیکون‌ها

منبع اصلی Iconها در:

```text
Logo&icon/
```

قرار دارد.

برای ساخت مجدد Iconها:

```bash
python scripts/make_icon.py
```

برای بررسی Iconها:

```bash
python scripts/icon_report.py
```

در صورت تغییر Icon، فایل‌های خروجی Build و Shortcutها باید مجدداً ساخته شوند.

---

## ⚠️ محدودیت‌های فعلی

* در هر لحظه یک فایل به‌عنوان عملیات اصلی انتقال پردازش می‌شود، اما Partهای آن به‌صورت Pipeline منتقل می‌شوند.
* فایل‌های بسیار کوچک، تا حدود 10MB، قابلیت Resume بین Restartها را ندارند.
* طول عمر دقیق Upload Session در Telegram مشخص نیست.
* Benchmarkها با Fake Transport انجام می‌شوند.
* تمرکز فعلی پروژه روی Windows است.

---

## 📚 مستندات

مستندات فنی و توسعه پروژه در پوشه زیر قرار دارند:

```text
docs/
```

از جمله:

* معماری
* Verification
* Packaging
* Development Notes
* گزارش‌های تست و بررسی

---

## 🧱 تکنولوژی‌های استفاده‌شده

* **Python**
* **PySide6**
* **Telethon**
* **SQLite**
* **Windows DPAPI**
* **Telegram MTProto**
* **Pillow**
* **pypdf**

---

## 📜 License

این پروژه تحت مجوز **MIT License** منتشر شده است.

---

## 💡 ایده اصلی

Teloude برای افرادی ساخته شده که می‌خواهند فایل‌های خودشان را در فضای Telegram خودشان نگهداری کنند، بدون اینکه فایل‌ها را به یک سرویس Cloud شخص ثالث بسپارند.

**فایل‌های شما، حساب Telegram شما، کنترل شما.**
