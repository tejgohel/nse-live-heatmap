import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")
import chartwin as c

UDD = r"C:\Users\t\AppData\Local\Google\Chrome\User Data"
UDD2 = r"C:\a b\User Data"

cases = [
    # profile name with a space, unquoted (what Windows actually showed)
    ("chrome.exe --profile-directory=Profile 4 --new-window https://x.com",
     ["--profile-directory=Profile 4"]),
    ('chrome.exe --profile-directory="Profile 4" https://x.com',
     ["--profile-directory=Profile 4"]),
    ("chrome.exe --profile-directory=Default https://x.com",
     ["--profile-directory=Default"]),
    # user-data-dir ending in "User Data" — a space, unquoted, then a flag
    (f"chrome.exe --user-data-dir={UDD} --type=renderer",
     [f"--user-data-dir={UDD}"]),
    # unquoted, then a URL and nothing else
    (f"chrome.exe --user-data-dir={UDD} https://x.com",
     [f"--user-data-dir={UDD}"]),
    # both, quoted path
    (f'chrome.exe --user-data-dir="{UDD2}" --profile-directory=Profile 5 https://y',
     [f"--user-data-dir={UDD2}", "--profile-directory=Profile 5"]),
    # nothing to find
    ("chrome.exe https://x.com", []),
]

bad = 0
for cmd, want in cases:
    got = c._parse_profile_flags(cmd)
    ok = got == want
    bad += 0 if ok else 1
    print(("  OK   " if ok else "  FAIL ") + str(got))
    if not ok:
        print("         expected: " + str(want))
print(f"\n  failures: {bad}")
