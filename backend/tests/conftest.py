"""テスト全体の後始末。"""

DATABASE_SKIP = "KOYORINA_TEST_DATABASE_URL"


def pytest_terminal_summary(terminalreporter):
    """実DBのテストを飛ばしたら、最後に目立つように伝える。

    APIテストの多くは実PostgreSQLが要る（test_api.py の context）。接続先を
    渡し忘れると100件以上が黙って skipped に紛れ、通ったように見えてしまう。
    """
    skipped = [report for report in terminalreporter.stats.get("skipped", [])
               if DATABASE_SKIP in str(getattr(report, "longrepr", ""))]
    if skipped:
        terminalreporter.write_sep("!", f"実DBのテスト {len(skipped)} 件を実行していません", yellow=True)
        terminalreporter.write_line(
            f"{DATABASE_SKIP} に移行済みの専用PostgreSQL（localhost）を指定すると実行します。README「検証」参照。")
