from pathlib import Path

path = Path("bot.py")
text = path.read_text(encoding="utf-8")
marker = "    elif message.chat.type == enums.ChatType.CHANNEL:\n"
insert = "        # Let command-specific handlers (/settings, /genlink, /batch, etc.)\n        # receive private text messages after this generic handler inspects them.\n        await message.continue_propagation()\n\n"
if insert not in text:
    if marker not in text:
        raise SystemExit("Could not find private/channel handler boundary")
    text = text.replace(marker, insert + marker, 1)
    path.write_text(text, encoding="utf-8")
    print("Patched command propagation")
else:
    print("Already patched")
