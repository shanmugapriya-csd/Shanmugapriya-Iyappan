#!/usr/bin/env python3
"""
Builds README.md from README.template.md using live data from your GitHub account:
  - name, bio, social links
  - pinned repos (falls back to most-starred)
  - recently active repos
  - language "stack" computed from your own repos
  - contribution totals + streaks

Needs: GITHUB_TOKEN (the Actions one is fine). Optional: PROFILE_TOKEN, a classic PAT
with read:user, if you want private contributions counted.
"""
import html
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "README.template.md"
OUTPUT = ROOT / "README.md"

TOKEN = os.environ.get("PROFILE_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
OWNER = os.environ.get("PROFILE_USER") or os.environ.get("GITHUB_REPOSITORY_OWNER", "")

PINK = "FF2BD6"
DARK = "1A0028"

# Languages you never want shown in the stack (e.g. {"Jupyter Notebook", "Dockerfile"})
IGNORE_LANGS = set()
MAX_LANGS = 12
MAX_REPOS_FOR_LANGS = 40

LOGO = {
    "C++": "cplusplus", "C#": "csharp", "Jupyter Notebook": "jupyter",
    "Shell": "gnubash", "Vue": "vuedotjs", "HTML": "html5", "CSS": "css3",
    "SCSS": "sass", "Dockerfile": "docker", "Objective-C": "apple",
    "TeX": "latex", "PowerShell": "powershell", "Vim Script": "vim",
    "Vim script": "vim", "Java": "openjdk", "Makefile": "gnu",
}

SOCIAL = {  # provider -> (label, simple-icons slug)
    "twitter": ("X / Twitter", "x"),
    "linkedin": ("LinkedIn", "linkedin"),
    "youtube": ("YouTube", "youtube"),
    "twitch": ("Twitch", "twitch"),
    "mastodon": ("Mastodon", "mastodon"),
    "bluesky": ("Bluesky", "bluesky"),
    "facebook": ("Facebook", "facebook"),
    "instagram": ("Instagram", "instagram"),
    "reddit": ("Reddit", "reddit"),
    "hometown": ("Hometown", "mastodon"),
    "npm": ("npm", "npm"),
    "generic": ("Link", ""),
}
SOCIAL_ORDER = ["twitter", "linkedin", "website", "youtube", "twitch"]


# ---------------------------------------------------------------- HTTP helpers
def _request(url, data=None):
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "profile-readme-builder",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def rest(path, params=None, default=None):
    url = "https://api.github.com" + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        return _request(url)
    except urllib.error.HTTPError as err:
        if default is not None:
            print(f"warn: {path} -> {err.code}", file=sys.stderr)
            return default
        raise


def paginate(path, params=None):
    page, out = 1, []
    while True:
        chunk = rest(path, {**(params or {}), "per_page": 100, "page": page})
        out.extend(chunk)
        if len(chunk) < 100:
            return out
        page += 1


def graphql(query, variables):
    res = _request("https://api.github.com/graphql", {"query": query, "variables": variables})
    if "errors" in res:
        raise RuntimeError(res["errors"])
    return res["data"]


# --------------------------------------------------------------------- helpers
def shield_escape(text):
    return urllib.parse.quote(text.replace("-", "--").replace("_", "__"), safe="")


def badge(label, message, logo=""):
    """Dark 'button' badge when message is empty, otherwise label (dark) + message (pink)."""
    logo_q = f"&logo={urllib.parse.quote(logo)}&logoColor={PINK}" if logo else ""
    if message:
        path = f"{shield_escape(label)}-{shield_escape(message)}-{PINK}"
    else:
        path = f"{shield_escape(label)}-{DARK}"
    return f"https://img.shields.io/badge/{path}?style=for-the-badge&labelColor={DARK}{logo_q}"


def lang_logo(name):
    return LOGO.get(name, name.lower().replace(" ", "").replace("+", "plus").replace("#", "sharp"))


def short(text, n):
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def streaks(days):
    counts = [d["contributionCount"] for d in days]
    longest = run = 0
    for c in counts:
        run = run + 1 if c > 0 else 0
        longest = max(longest, run)
    i, current = len(counts) - 1, 0
    if i >= 0 and counts[i] == 0:  # today may not have contributions yet
        i -= 1
    while i >= 0 and counts[i] > 0:
        current += 1
        i -= 1
    return current, longest


# ------------------------------------------------------------------- data pulls
def load_data():
    user = rest(f"/users/{OWNER}")
    socials = rest(f"/users/{OWNER}/social_accounts", default=[])
    repos = [
        r for r in paginate(f"/users/{OWNER}/repos", {"type": "owner", "sort": "pushed"})
        if not r["fork"] and not r["archived"] and r["name"].lower() != OWNER.lower()
    ]

    langs = Counter()
    topics = Counter()
    for r in repos[:MAX_REPOS_FOR_LANGS]:
        for lang, size in rest(f"/repos/{OWNER}/{r['name']}/languages", default={}).items():
            if lang not in IGNORE_LANGS:
                langs[lang] += size
        for t in r.get("topics", []):
            topics[t] += 1

    gql = graphql(
        """
        query($login: String!) {
          user(login: $login) {
            pinnedItems(first: 6, types: REPOSITORY) {
              nodes { ... on Repository {
                name description url stargazerCount forkCount
                primaryLanguage { name }
              } }
            }
            contributionsCollection {
              totalCommitContributions
              totalPullRequestContributions
              totalIssueContributions
              totalPullRequestReviewContributions
              contributionCalendar {
                totalContributions
                weeks { contributionDays { contributionCount } }
              }
            }
          }
        }
        """,
        {"login": OWNER},
    )["user"]

    return user, socials, repos, langs, topics, gql


# ---------------------------------------------------------------- renderers
def render_socials(user, socials):
    found = {}
    for s in socials:
        prov = s.get("provider", "generic")
        found.setdefault(prov, s["url"])
    if user.get("twitter_username"):
        found.setdefault("twitter", f"https://x.com/{user['twitter_username']}")
    blog = (user.get("blog") or "").strip()
    if blog:
        found["website"] = blog if blog.startswith("http") else f"https://{blog}"

    order = [p for p in SOCIAL_ORDER if p in found] + [p for p in found if p not in SOCIAL_ORDER]
    parts = []
    for prov in order:
        label, slug = ("Website", "googlechrome") if prov == "website" else SOCIAL.get(prov, (prov.title(), ""))
        parts.append(f'<a href="{html.escape(found[prov])}"><img src="{badge(label, "", slug)}" alt="{label}" /></a>')
    parts.append(f'<a href="https://github.com/{OWNER}"><img src="{badge("GitHub", "", "github")}" alt="GitHub" /></a>')
    return "\n".join(parts)


def render_stack(langs, topics):
    total = sum(langs.values()) or 1
    lines = []
    for lang, size in langs.most_common(MAX_LANGS):
        pct = size / total * 100
        label = f"{pct:.0f}%" if pct >= 1 else "<1%"
        lines.append(f'<img src="{badge(lang, label, lang_logo(lang))}" alt="{lang} {label}" />')
    out = "\n".join(lines)
    if topics:
        chips = " ".join(f"`{t}`" for t, _ in topics.most_common(12))
        out += f"\n\n<sub>Topics across my repos:</sub><br />\n{chips}"
    return out


def render_contrib(gql, repos, user):
    cc = gql["contributionsCollection"]
    cal = cc["contributionCalendar"]
    days = [d for w in cal["weeks"] for d in w["contributionDays"]]
    cur, longest = streaks(days)
    stars = sum(r["stargazers_count"] for r in repos)

    cells = [
        (cal["totalContributions"], "Contributions<br />(last year)"),
        (cc["totalCommitContributions"], "Commits"),
        (cc["totalPullRequestContributions"], "Pull requests"),
        (cc["totalIssueContributions"], "Issues"),
        (cc["totalPullRequestReviewContributions"], "Reviews"),
        (stars, "Stars earned"),
        (cur, "Current streak<br />(days)"),
        (longest, "Longest streak<br />(days)"),
        (user["public_repos"], "Public repos"),
        (user["followers"], "Followers"),
    ]
    rows = []
    for i in range(0, len(cells), 5):
        tds = "".join(
            f'<td align="center"><h2>{n:,}</h2><sub>{label}</sub></td>' for n, label in cells[i:i + 5]
        )
        rows.append(f"<tr>{tds}</tr>")
    return '<table align="center" width="100%">\n' + "\n".join(rows) + "\n</table>"


def repo_card(name, url, desc, lang, stars, forks):
    meta = " · ".join(
        x for x in [f"● {lang}" if lang else "", f"⭐ {stars}" if stars else "", f"🍴 {forks}" if forks else ""] if x
    )
    return (
        f'<td valign="top" width="50%">\n'
        f'<h4><a href="{url}">{html.escape(name)}</a></h4>\n'
        f"<p>{html.escape(short(desc, 110)) or '<i>No description yet</i>'}</p>\n"
        f"<sub>{meta}</sub>\n</td>"
    )


def pick_featured(gql, repos):
    pinned = [n for n in gql["pinnedItems"]["nodes"] if n]
    if pinned:
        return [
            dict(name=n["name"], url=n["url"], desc=n["description"],
                 lang=(n["primaryLanguage"] or {}).get("name"), stars=n["stargazerCount"], forks=n["forkCount"])
            for n in pinned
        ]
    top = sorted(repos, key=lambda r: r["stargazers_count"], reverse=True)[:6]
    return [
        dict(name=r["name"], url=r["html_url"], desc=r["description"], lang=r["language"],
             stars=r["stargazers_count"], forks=r["forks_count"])
        for r in top
    ]


def render_cards(items):
    cards = [repo_card(i["name"], i["url"], i["desc"], i["lang"], i["stars"], i["forks"]) for i in items]
    rows = []
    for i in range(0, len(cards), 2):
        pair = cards[i:i + 2]
        if len(pair) == 1:
            pair.append("<td></td>")
        rows.append("<tr>\n" + "\n".join(pair) + "\n</tr>")
    return '<table align="center" width="100%">\n' + "\n".join(rows) + "\n</table>"


def render_recent(repos):
    lines = []
    for r in repos[:5]:
        when = r["pushed_at"][:10]
        lines.append(f"- [**{r['name']}**]({r['html_url']}): {short(r['description'], 90) or 'No description'} <sub>· updated {when}</sub>")
    return "\n".join(lines)


def typing_lines(name, bio):
    lines = [f"Hi, I'm {name}"]
    if bio:
        lines.append(short(bio, 55))
    lines.append("Welcome to my corner of the internet")
    return urllib.parse.quote(";".join(lines), safe=";")


# ----------------------------------------------------------------------- main
def main():
    if not OWNER:
        sys.exit("Set PROFILE_USER (or run inside GitHub Actions).")
    if not TOKEN:
        sys.exit("Set GITHUB_TOKEN (the contribution query needs authentication).")

    user, socials, repos, langs, topics, gql = load_data()
    featured = pick_featured(gql, repos)
    flagship = featured[0] if featured else dict(name="", url=f"https://github.com/{OWNER}", desc="")

    name = user.get("name") or OWNER
    bio = user.get("bio") or ""

    values = {
        "{{USERNAME}}": OWNER,
        "{{NAME}}": html.escape(name),
        "{{BIO}}": html.escape(bio),
        "{{TYPING}}": typing_lines(name, bio).replace("+", "%20"),
        "{{SOCIALS}}": render_socials(user, socials),
        "{{STACK}}": render_stack(langs, topics),
        "{{CONTRIB}}": render_contrib(gql, repos, user),
        "{{FEATURED}}": render_cards(featured),
        "{{RECENT}}": render_recent(repos),
        "{{FLAGSHIP_NAME}}": html.escape(flagship["name"]),
        "{{FLAGSHIP_URL}}": flagship["url"],
        "{{FLAGSHIP_DESC}}": html.escape(short(flagship["desc"], 120)),
    }

    text = TEMPLATE.read_text(encoding="utf-8")
    for key, val in values.items():
        text = text.replace(key, val)
    OUTPUT.write_text(text, encoding="utf-8")
    print(f"Wrote {OUTPUT} for {OWNER}: {len(repos)} repos, {len(langs)} languages")


if __name__ == "__main__":
    main()