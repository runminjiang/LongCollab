class Exhausted(ValueError):
    """剩余额度不足以继续模型请求。"""


def record(db, actor, category, tokens=0, usd=None, *, cached_tokens=0):
    if type(tokens) is not int or tokens < 0:
        raise ValueError("无效的 token 用量")
    if type(cached_tokens) is not int or not 0 <= cached_tokens <= tokens:
        raise ValueError("无效的缓存 token 用量")
    db.execute(
        "INSERT INTO usage(actor,category,tokens,usd,cached_tokens) VALUES(?,?,?,?,?)",
        (actor, category, tokens, usd, cached_tokens),
    )


def total(db):
    return db.execute(
        "SELECT COALESCE(SUM(tokens),0) FROM usage WHERE category!='judge'"
    ).fetchone()[0]


def agent_used(db, actor):
    return db.execute(
        "SELECT COALESCE(SUM(tokens-cached_tokens),0) FROM usage WHERE actor=? AND category='worker'",
        (actor,),
    ).fetchone()[0]


def summary(db):
    return [
        dict(row)
        for row in db.execute("""
        SELECT actor,category,SUM(tokens) AS tokens,SUM(cached_tokens) AS cached_tokens,
               CASE WHEN COUNT(usd)=COUNT(*) THEN SUM(usd) END AS usd
        FROM usage GROUP BY actor,category ORDER BY category,actor
    """)
    ]
