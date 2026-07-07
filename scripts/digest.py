#!/usr/bin/env python3
"""GitHub Trending Digest — search, report, deliver via email."""

import argparse
import os
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
import yaml


def load_config(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def search_github(query: str, min_stars: int, date_field: str | None,
                  date_val: str | None, per_page: int = 30) -> list[dict]:
    """Search GitHub repositories with a raw query string, sorted by stars."""
    url = "https://api.github.com/search/repositories"

    qualifiers = [query,
                  f"stars:>={min_stars}"]
    if date_field and date_val:
        qualifiers.append(f"{date_field}:>={date_val}")

    params = {
        "q": " ".join(qualifiers),
        "sort": "stars",
        "order": "desc",
        "per_page": per_page,
    }

    headers = {"Accept": "application/vnd.github.v3+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    resp = requests.get(url, params=params, headers=headers, timeout=30)
    if resp.status_code != 200:
        print(f"  Search failed: HTTP {resp.status_code} {resp.text[:200]}")
        return []

    data = resp.json()
    repos = []
    for item in data.get("items", []):
        repos.append({
            "full_name": item["full_name"],
            "name": item["name"],
            "description": (item.get("description") or "").strip(),
            "stars": item["stargazers_count"],
            "language": item.get("language") or "N/A",
            "url": item["html_url"],
            "topics": item.get("topics", []),
            "updated_at": item["updated_at"],
            "created_at": item["created_at"],
            "pushed_at": item["pushed_at"],
        })
    return repos


def search_all_keywords(keywords: list[str], min_stars: int, mode: str,
                        max_results: int) -> list[dict]:
    """Search across batched keyword groups, deduplicate, return top results by stars."""
    seen: set[str] = set()
    all_repos: list[dict] = []

    now = datetime.now(timezone.utc)
    date_str = None
    date_field = None

    if mode == "daily":
        date_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        date_field = "pushed"
    elif mode == "weekly":
        date_str = (now - timedelta(days=7)).strftime("%Y-%m-%d")
        date_field = "pushed"

    # Batch keywords into groups of 4 with OR to stay within rate limits.
    # Unauthenticated: 10 req/min → 3 batches with 7s spacing = safe.
    batch_size = 4
    batches = [keywords[i:i + batch_size] for i in range(0, len(keywords), batch_size)]

    for batch in batches:
        or_clause = " OR ".join(f'"{kw}"' if " " in kw else kw for kw in batch)
        query = f"({or_clause}) in:name,description,topics"

        per_page = max(30, max_results)
        repos = search_github(query, min_stars, date_field, date_str, per_page=per_page)
        for repo in repos:
            if repo["full_name"] not in seen:
                seen.add(repo["full_name"])
                all_repos.append(repo)
        # 3 batches × 7s = 21s → well within 10 req/min unauthenticated
        time.sleep(7)

    all_repos.sort(key=lambda r: r["stars"], reverse=True)
    return all_repos[:max_results]


def generate_report(repos: list[dict], mode: str) -> str:
    """Generate Chinese markdown report from repo list."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    if mode == "daily":
        title = f"GitHub 学术研究工具 · 日报 | {datetime.now().strftime('%Y-%m-%d')}"
        period = "过去 24 小时"
    else:
        week_num = datetime.now().isocalendar()[1]
        title = f"GitHub 学术研究工具 · 周报 | 第 {week_num} 周"
        period = "本周"

    lines = [
        f"# {title}",
        "",
        f"**生成时间**: {now_str} &nbsp;|&nbsp; **收录**: {len(repos)} 个仓库",
        "",
        "---",
        "",
    ]

    if not repos:
        lines.append(f"_{period}暂无符合条件的高星标仓库。_")
        return "\n".join(lines)

    total_stars = sum(r["stars"] for r in repos)
    languages = sorted({r["language"] for r in repos if r["language"] != "N/A"})
    lang_str = ", ".join(languages) if languages else "多种语言"

    lines.append(
        f"**{period}共收录 {len(repos)} 个仓库，"
        f"总计 {total_stars:,} 星标** &nbsp;|&nbsp; 主要语言: {lang_str}"
    )
    lines.append("")
    lines.append("---")
    lines.append("")

    for i, repo in enumerate(repos, 1):
        desc = repo["description"] or "(无描述)"
        if len(desc) > 200:
            desc = desc[:200] + "..."

        topics_str = ""
        if repo["topics"]:
            top_topics = repo["topics"][:5]
            topics_str = " `" + "` `".join(top_topics) + "`"

        lines.append(f"### {i}. [{repo['full_name']}]({repo['url']})")
        lines.append(f"⭐ **{repo['stars']:,}** stars")
        lines.append(f"{desc}")
        lines.append(
            f"🔧 语言: {repo['language']} &nbsp;|&nbsp; "
            f"最近更新: {repo['updated_at'][:10]}"
        )
        if topics_str:
            lines.append(f"🏷️{topics_str}")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(f"*由 GitHub Trends Digest 自动生成 · {now_str}*")

    return "\n".join(lines)


def send_email(report_md: str, config: dict, mode: str) -> None:
    """Send report via SMTP."""
    smtp_host = os.environ.get("SMTP_HOST") or config["email"]["smtp_host"]
    smtp_port = int(os.environ.get("SMTP_PORT") or config["email"]["smtp_port"])
    smtp_user = os.environ["SMTP_USER"]
    smtp_pass = os.environ["SMTP_PASS"]
    email_to = os.environ["EMAIL_TO"]
    from_name = config["email"]["from_name"]

    subject = (
        f"GitHub 学术研究工具 · {'日报' if mode == 'daily' else '周报'}"
        f" | {datetime.now().strftime('%Y-%m-%d')}"
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{smtp_user}>"
    msg["To"] = email_to

    msg.attach(MIMEText(report_md, "plain", "utf-8"))

    html_body = _md_to_html(report_md)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
        server.ehlo()
        if config["email"]["use_tls"]:
            server.starttls()
            server.ehlo()
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, email_to, msg.as_string())

    print(f"Email sent to {email_to}")


def _md_to_html(md_text: str) -> str:
    """Convert markdown to basic HTML for email."""
    lines = md_text.split("\n")
    html_lines = []
    in_list = False

    for line in lines:
        if line.startswith("# "):
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append(
                f'<h1 style="color:#1a1a1a;border-bottom:2px solid #0366d6;'
                f'padding-bottom:8px">{line[2:]}</h1>'
            )
        elif line.startswith("### "):
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append(
                f'<h3 style="color:#0366d6;margin-top:24px">{line[4:]}</h3>'
            )
        elif line.startswith("⭐"):
            html_lines.append(
                f'<p style="color:#24292e;font-weight:bold;margin:4px 0">{line}</p>'
            )
        elif line.startswith("- "):
            if not in_list:
                html_lines.append('<ul style="padding-left:20px">')
                in_list = True
            html_lines.append(f"<li>{line[2:]}</li>")
        elif line.strip() == "---":
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append('<hr style="border:1px solid #e1e4e8;margin:20px 0">')
        elif line.strip() == "":
            html_lines.append("<br>")
        elif line.startswith("*由 GitHub"):
            html_lines.append(
                f'<p style="color:#6a737d;font-size:12px;margin-top:20px">{line}</p>'
            )
        else:
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append(f"<p>{line}</p>")

    if in_list:
        html_lines.append("</ul>")

    body = "\n".join(html_lines)
    return (
        '<html><body style="font-family:-apple-system,BlinkMacSystemFont,'
        '"Segoe UI",Helvetica,Arial,sans-serif;max-width:780px;'
        "padding:24px;color:#24292e;line-height:1.6\">"
        + body
        + "</body></html>"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="GitHub Trending Digest")
    parser.add_argument("--mode", choices=["daily", "weekly"], default="daily")
    parser.add_argument("--dry-run", action="store_true",
                        help="Generate report but don't send email")
    parser.add_argument("--config", default=None, help="Path to config.yml")
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent
    config_path = Path(args.config) if args.config else script_dir / "config.yml"
    config = load_config(config_path)

    cfg = config["search"]
    report_cfg = config["report"]

    min_stars = cfg["min_stars_daily"] if args.mode == "daily" else cfg["min_stars_weekly"]
    max_results = report_cfg["daily_max_repos"] if args.mode == "daily" else report_cfg["weekly_max_repos"]

    print(f"Mode: {args.mode} | Min stars: {min_stars} | Max results: {max_results}")
    print(f"Searching {len(cfg['keywords'])} keywords: {', '.join(cfg['keywords'])}")
    print()

    repos = search_all_keywords(cfg["keywords"], min_stars, args.mode, max_results)
    print(f"Found {len(repos)} unique repos after dedup")
    for r in repos:
        print(f"  ⭐{r['stars']:,}  {r['full_name']}")

    report = generate_report(repos, args.mode)

    report_dir = script_dir.parent / "reports"
    report_dir.mkdir(exist_ok=True)

    if args.mode == "daily":
        filename = f"daily-{datetime.now().strftime('%Y-%m-%d')}.md"
    else:
        filename = f"weekly-{datetime.now().strftime('%Y')}-W{datetime.now().isocalendar()[1]}.md"

    report_path = report_dir / filename
    report_path.write_text(report, encoding="utf-8")
    print(f"\nReport saved: {report_path}")

    if not args.dry_run:
        print("Sending email...")
        send_email(report, config, args.mode)
    else:
        print("\n[DRY RUN] Skipping email delivery")
        print()
        print("=" * 60)
        print(report)


if __name__ == "__main__":
    main()
