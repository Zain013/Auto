# AUTO REQUEST ACCEPT BOT — Setup Guide

## 1. Install
```
pip install -r requirements.txt
```

## 2. Master Bot Token
`bot.py` file kholo, top me:
```python
MASTER_BOT_TOKEN = "YOUR_MASTER_BOT_TOKEN_HERE"
```
Yahan BotFather se mila token daalo.

## 3. Run
```
python bot.py
```

## 4. Owner Banna
Master bot ko Telegram me `/start` karo — jo pehla insaan `/start` karega,
wahi **OWNER** ban jayega. Yeh permanently `data.json` me save ho jata hai.

## 5. Channel Setup (Telegram side)
1. Bot ko apne channel/group me **Admin** banao — "Invite Users via Link"
   permission zaroor ON rakho.
2. Channel Settings → "Approve New Members" (join request approval) ON karo.
   Agar yeh OFF hai to koi bhi seedha join ho jayega, request generate hi
   nahi hogi.

## 6. Bot Menu se Channel Add Karo
`/start` karo → **📡 My Channels** → **➕ Add Channel** →
- Channel se koi bhi message **forward** karo (sabse reliable), YA
- Channel ka `@username` bhej do.

## 7. Features
| Feature | Kahan milega |
|---|---|
| Auto Accept ON/OFF | Channel detail page ka toggle button |
| Pending Requests count (auto-refresh) | Channel detail → ⏳ Pending Requests |
| Instant welcome message | Menu → ✏️ Welcome Message (edit karne ke liye) |
| Add/Delete Channel | 📡 My Channels page |
| Add/Remove Child Bot | Sirf Owner, Master bot ke menu me |

## 8. Child Bots
Owner **➕ Add Child Bot** dabakar ek naya BotFather token de sakta hai.
Wo bot turant automatically start ho jata hai, same features ke saath
(channels, auto-accept, welcome message) — lekin us child bot ke andar
"Add/Remove Child" option nahi aayega, wo sirf Master bot me hai.

Owner id same rehti hai sabhi bots ke liye — jis Telegram account ne
Master bot ka owner ban kar setup kiya, wahi sab child bots ko bhi
control kar sakta hai (unke apne /start se).

## Notes
- Data `data.json` file me save hota hai (channels, pending requests,
  welcome message, child tokens). Ise backup me rakho.
- Server 24x7 chalu rehna chahiye taaki bot instant respond kare —
  VPS ya Railway/Render jaisi jagah host karo.
