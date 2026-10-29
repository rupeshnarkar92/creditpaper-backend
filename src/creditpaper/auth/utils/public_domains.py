PUBLIC_DOMAINS = {
    "gmail.com",
    "yahoo.com",
    "yahoo.co.in",
    "yahoo.co.uk",
    "outlook.com",
    "hotmail.com",
    "live.com",
    "live.co.uk",
    "aol.com",
    "icloud.com",
    "mail.com",
    "protonmail.com",
    "protonmail.ch",
    "zoho.com",
    "yandex.com",
    "yandex.ru",
    "qq.com",
    "163.com",
    "126.com",
    "sina.com",
    "foxmail.com",
    "gmx.com",
    "gmx.net",
    " tutanota.com",
    "tutanota.de",
    "mailbox.org",
    "fastmail.com",
    "pm.me",
    "cock.li",
    "disroot.org",
    "riseup.net",
}


def is_public_domain(email: str) -> bool:
    domain = email.split("@")[1].lower().strip()
    return domain in PUBLIC_DOMAINS
