# Gym Membership Management Bot - Setup Guide

## 1. Requirements install karein
```bash
pip install -r requirements.txt
```

## 2. Bot banayein (agar pehle se nahi hai)
1. Telegram par `@BotFather` ko message karein
2. `/newbot` bhejें aur naam set karein
3. Aapko ek **bot token** milega, jaisa: `123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxx`

## 3. Apna Telegram ID (aur baaki admins/owners ka ID) pata karein
1. Telegram par `@userinfobot` ko message karein
2. Wo aapko aapka numeric chat ID dega
3. Har admin/owner se yeh id le lein jinhe expiry notification chahiye

## 4. Configuration set karein
Terminal me ye run karein (apna token aur IDs daal ke):
```bash
export GYM_BOT_TOKEN="123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxx"
export GYM_BOT_ADMINS="111111111,222222222"
```
> Multiple admin/owner IDs ko comma se separate karein, spaces mat dein.

(Alternative: `main.py` file khol ke `BOT_TOKEN` aur `ADMIN_IDS` seedhe edit kar sakte hain.)

### Railway.app par deploy kar rahe ho?
Railway ki filesystem **ephemeral** hoti hai — matlab redeploy/restart hone par
`members.json` delete ho sakta hai. Isliye:
- `GYM_BOT_TOKEN` aur `GYM_BOT_ADMINS` ko Railway ke **Variables** tab me set karein
  (env variable se hi, hardcode mat karein)
- Regularly `/backup` command chalate rahein aur file safe jagah save karein
- Agar data reset ho jaye kabhi, `/restore` se turant wapas load kar sakte hain
- Service type **"Worker"** rakhna, "Web Service" nahi — bot koi port pe listen
  nahi karta, isliye web health-check fail ho sakta hai

## 5. Bot run karein
```bash
python main.py
```
Bot chalte hi automatically har 24 ghante me sab members ki expiry date check karega.
Jis din kisi member ki expiry date aayegi, sab admins/owners ko turant message chala jayega:

```
Aditya (7992357603) - Subscription expired on 12/07/2026
```

## Commands (sirf admin/owner use kar sakte hain)

| Command | Kaam |
|---|---|
| `/add <name> <number> <dd/mm/yyyy> [duration]` | Naya member add karta hai. Duration optional hai. |
| `/edit <number> <duration>` | Member ka subscription plan/mode change karta hai (aage renewals bhi isi duration se honge). |
| `/members` | Total count aur sabhi members ki compact list dikhata hai. |
| `/due` | Jinka subscription expire ho chuka hai unki list. |
| `/paid <number>` | Number se member dhoond kar uske plan ke hisaab se renew karta hai. |
| `/delete <number>` | Sirf number dalke member ko permanently remove karta hai. |
| `/backup` | Current data (`members.json`) ko file ke roop me bhejta hai. |
| `/restore` | Pehle liya gaya backup file wapas load karne ke liye. |

### Duration format
`1m`, `3m`, `6m`, `12m` — ya likhna chahein to `1month`, `3months` bhi chalega.
Agar `/add` me duration diya hi nahi, toh default **1 month** lagega.

### Examples

Simple add (default 1 month):
```
/add Aditya 7992357603 12/06/2026
```
→ Expiry `12/07/2026` set hogi.

Add with custom plan (e.g. 3 month membership):
```
/add Aditya 7992357603 12/06/2026 3m
```
→ Expiry `12/09/2026` set hogi (3 month aage).

Existing member ka plan change karna (monthly se quarterly):
```
/edit 7992357603 3m
```
→ Uski expiry date turant unke original join date + 3 month se recalculate ho jayegi,
aur agli baar jab bhi `/paid` karoge, wo automatically 3 month hi renew karega
(jab tak dobara `/edit` na kar do).

Payment mark karna (member ke current plan ke hisaab se auto renew):
```
/paid 7992357603
```
→ Hamesha **purani expiry date se +plan duration** hoga (simple math), chahe member
kitna bhi late pay kare. Agar plan 1m hai to 1 month aage badhega, agar 3m hai to
3 month aage badhega — sirf number chahiye, naam yaad rakhne ki zarurat nahi.

Backup lena:
```
/backup
```
→ Bot aapko `members.json` ek file ke roop me bhej dega, jise aap safe jagah save
kar lo (Google Drive, laptop, wahin pe).

Backup restore karna:
```
/restore
```
→ Bot poochega "please send the backup file". Uske baad wahi `.json` file bot ko
document ke roop me bhej do, data wapas load ho jayega.

### Duplicate naam handling
Agar do members ka naam same hai (jaise dono "Aditya"), toh bot khud dusre wale ko
`Aditya 1`, phir agla `Aditya 2` naam de dega — automatic, kuch karne ki zarurat nahi.

## Demo Output (bot exactly aisa reply karega)

**`/add Aditya 7992357603 12/06/2026`**
```
Member added successfully.

Name: Aditya
Number: 7992357603
Joined: 12/06/2026
Plan: 1 month
Expiry: 12/07/2026
```

**`/add Aditya 7992357602 15/06/2026`** (naam already exist karta hai)
```
Member added successfully.

Name: Aditya 1
Number: 7992357602
Joined: 15/06/2026
Plan: 1 month
Expiry: 15/07/2026
```

**`/add Rohit 9876543210 01/07/2026 3m`**
```
Member added successfully.

Name: Rohit
Number: 9876543210
Joined: 01/07/2026
Plan: 3 months
Expiry: 01/10/2026
```

**`/members`**
```
Total Members: 3

1. Aditya | 7992357603 | Expiry: 12/07/2026
2. Aditya 1 | 7992357602 | Expiry: 15/07/2026
3. Rohit | 9876543210 | Expiry: 01/10/2026
```

**`/due`** (jab kisi ka subscription khatam ho chuka ho)
```
Members with Expired Subscriptions:

1. Aditya (7992357603) - Expired on 12/07/2026
```
Agar koi due nahi hai:
```
No members are currently due. All subscriptions are active.
```

**`/paid 7992357603`** (agar expiry thi 12/07/2026)
```
Payment recorded successfully.

Name: Aditya
Number: 7992357603
Plan: 1 month
New Expiry Date: 12/08/2026
```

**`/edit 7992357603 3m`**
```
Subscription plan updated successfully.

Name: Aditya
Number: 7992357603
New Plan: 3 months (applies from every future renewal too)
Recalculated Expiry: 12/09/2026
```

**`/delete 7992357603`**
```
Member removed successfully.

Name: Aditya
Number: 7992357603
```
Agar number exist hi nahi karta:
```
No member found with number 7992357603.
```

**Automatic expiry notification** (jo admins/owners ko apne aap milega, jis din expiry ho)
```
Aditya (7992357603) - Subscription expired on 12/07/2026
```

**`/backup`**
```
[Bot ek file bhejega: members_backup_20260729_143000.json]
Here is your current member data backup.
Save this file safely. Use /restore to load it back into the bot later.
```

**`/restore`**
```
Please send the backup file (.json) now as a document to restore your data.
This will overwrite the current member data.
```
Uske baad file bhejne par:
```
Data restored successfully. 3 member record(s) loaded.
```

**Agar koi non-admin command chalaye**
```
You are not authorized to use this bot. Please contact the gym admin.
```

## Data storage
Sab member data `members.json` file me save hota hai (isi folder me, script ke saath).
Railway jaise platforms par yeh permanent nahi hai (redeploy/restart pe delete ho
sakta hai), isliye `/backup` regularly lete rehna zaroori hai.

## Deployment note
Yeh script `python main.py` se local machine ya kisi bhi VPS/server par continuously
chalate rehna hoga (background me, jaise `screen`, `tmux`, `systemd`, ya Railway ke
"Worker" process ke through) taaki daily expiry check aur notifications chalte rahein.
