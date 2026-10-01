-- 0001_init: core schema.
-- Conventions:
--   * money is stored as INTEGER cents (never floats) to avoid rounding drift
--   * timestamps are ISO-8601 UTC text, dates are 'YYYY-MM-DD' text
--   * every table that the UI edits has created_at / updated_at

-- ---------------------------------------------------------------- tags
CREATE TABLE tags (
    id    INTEGER PRIMARY KEY,
    name  TEXT NOT NULL UNIQUE COLLATE NOCASE,
    color TEXT NOT NULL DEFAULT '#6b7280'
);

-- ------------------------------------------------------------ pictures
-- Files live on disk (data/media), the DB holds metadata + content hash.
-- sha256 is UNIQUE so the same image uploaded twice is stored once.
CREATE TABLE pictures (
    id            INTEGER PRIMARY KEY,
    sha256        TEXT NOT NULL UNIQUE,
    stored_name   TEXT NOT NULL,
    original_name TEXT NOT NULL,
    mime          TEXT NOT NULL,
    size_bytes    INTEGER NOT NULL CHECK (size_bytes >= 0),
    width         INTEGER,
    height        INTEGER,
    taken_at      TEXT,
    camera        TEXT,
    title         TEXT NOT NULL DEFAULT '',
    description   TEXT NOT NULL DEFAULT '',
    album         TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX idx_pictures_album ON pictures(album);
CREATE INDEX idx_pictures_taken ON pictures(taken_at);

-- ------------------------------------------------------------ contacts
CREATE TABLE contacts (
    id          INTEGER PRIMARY KEY,
    first_name  TEXT NOT NULL CHECK (length(trim(first_name)) > 0),
    last_name   TEXT NOT NULL DEFAULT '',
    company     TEXT NOT NULL DEFAULT '',
    job_title   TEXT NOT NULL DEFAULT '',
    email       TEXT NOT NULL DEFAULT '',
    phone       TEXT NOT NULL DEFAULT '',
    address     TEXT NOT NULL DEFAULT '',
    city        TEXT NOT NULL DEFAULT '',
    region      TEXT NOT NULL DEFAULT '',
    postal_code TEXT NOT NULL DEFAULT '',
    country     TEXT NOT NULL DEFAULT '',
    birthday    TEXT CHECK (birthday IS NULL OR birthday GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    website     TEXT NOT NULL DEFAULT '',
    notes       TEXT NOT NULL DEFAULT '',
    favorite    INTEGER NOT NULL DEFAULT 0 CHECK (favorite IN (0, 1)),
    photo_id    INTEGER REFERENCES pictures(id) ON DELETE SET NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX idx_contacts_name ON contacts(last_name COLLATE NOCASE, first_name COLLATE NOCASE);
CREATE INDEX idx_contacts_email ON contacts(email COLLATE NOCASE);

-- many-to-many: contacts <-> tags
CREATE TABLE contact_tags (
    contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    tag_id     INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (contact_id, tag_id)
) WITHOUT ROWID;
CREATE INDEX idx_contact_tags_tag ON contact_tags(tag_id);

-- ------------------------------------------------------------ expenses
CREATE TABLE expense_categories (
    id                   INTEGER PRIMARY KEY,
    name                 TEXT NOT NULL UNIQUE COLLATE NOCASE,
    color                TEXT NOT NULL DEFAULT '#6b7280',
    monthly_budget_cents INTEGER CHECK (monthly_budget_cents IS NULL OR monthly_budget_cents >= 0)
);

CREATE TABLE expenses (
    id                 INTEGER PRIMARY KEY,
    spent_on           TEXT NOT NULL CHECK (spent_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    amount_cents       INTEGER NOT NULL CHECK (amount_cents >= 0),
    currency           TEXT NOT NULL DEFAULT 'USD' CHECK (length(currency) = 3),
    merchant           TEXT NOT NULL DEFAULT '',
    category_id        INTEGER REFERENCES expense_categories(id) ON DELETE SET NULL,
    payment_method     TEXT NOT NULL DEFAULT '',
    description        TEXT NOT NULL DEFAULT '',
    contact_id         INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
    receipt_picture_id INTEGER REFERENCES pictures(id) ON DELETE SET NULL,
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX idx_expenses_date ON expenses(spent_on);
CREATE INDEX idx_expenses_category_date ON expenses(category_id, spent_on);

-- --------------------------------------------------------------- codes
CREATE TABLE codes (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('qr', 'ean13', 'ean8', 'upca', 'code128', 'code39', 'isbn13')),
    payload     TEXT NOT NULL CHECK (length(payload) > 0),
    label       TEXT NOT NULL DEFAULT '',
    notes       TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT 'generated' CHECK (source IN ('generated', 'scanned', 'imported')),
    scan_count  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (kind, payload)
);

-- ---------------------------------------------------- custom sections
-- User-defined "tables" without DDL at runtime: a section declares typed
-- fields, records store their values as a validated JSON document.
CREATE TABLE sections (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    slug        TEXT NOT NULL UNIQUE CHECK (slug GLOB '[a-z0-9]*' AND slug NOT GLOB '*[^a-z0-9-]*'),
    icon        TEXT NOT NULL DEFAULT '📁',
    description TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE section_fields (
    id         INTEGER PRIMARY KEY,
    section_id INTEGER NOT NULL REFERENCES sections(id) ON DELETE CASCADE,
    key        TEXT NOT NULL CHECK (key GLOB '[a-z_]*' AND key NOT GLOB '*[^a-z0-9_]*'),
    label      TEXT NOT NULL,
    type       TEXT NOT NULL CHECK (type IN ('text', 'longtext', 'number', 'money', 'date', 'boolean',
                                             'select', 'url', 'email', 'picture', 'contact', 'code')),
    required   INTEGER NOT NULL DEFAULT 0 CHECK (required IN (0, 1)),
    options    TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(options)),
    position   INTEGER NOT NULL DEFAULT 0,
    UNIQUE (section_id, key)
);

CREATE TABLE section_records (
    id         INTEGER PRIMARY KEY,
    section_id INTEGER NOT NULL REFERENCES sections(id) ON DELETE CASCADE,
    data       TEXT NOT NULL CHECK (json_valid(data) AND json_type(data) = 'object'),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX idx_section_records_section ON section_records(section_id);

-- ------------------------------------------------------- saved queries
CREATE TABLE saved_queries (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    sql         TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- --------------------------------------------- updated_at maintenance
CREATE TRIGGER trg_contacts_touch AFTER UPDATE ON contacts WHEN NEW.updated_at = OLD.updated_at
BEGIN UPDATE contacts SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = NEW.id; END;
CREATE TRIGGER trg_expenses_touch AFTER UPDATE ON expenses WHEN NEW.updated_at = OLD.updated_at
BEGIN UPDATE expenses SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = NEW.id; END;
CREATE TRIGGER trg_pictures_touch AFTER UPDATE ON pictures WHEN NEW.updated_at = OLD.updated_at
BEGIN UPDATE pictures SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = NEW.id; END;
CREATE TRIGGER trg_codes_touch AFTER UPDATE ON codes WHEN NEW.updated_at = OLD.updated_at
BEGIN UPDATE codes SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = NEW.id; END;
CREATE TRIGGER trg_section_records_touch AFTER UPDATE ON section_records WHEN NEW.updated_at = OLD.updated_at
BEGIN UPDATE section_records SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = NEW.id; END;

-- -------------------------------------------------------------- views
CREATE VIEW v_contacts AS
SELECT c.*,
       trim(c.first_name || ' ' || c.last_name) AS full_name,
       COALESCE((SELECT json_group_array(json_object('id', t.id, 'name', t.name, 'color', t.color))
                   FROM contact_tags ct JOIN tags t ON t.id = ct.tag_id
                  WHERE ct.contact_id = c.id), '[]') AS tags_json
  FROM contacts c;

CREATE VIEW v_expenses AS
SELECT e.*,
       e.amount_cents / 100.0 AS amount,
       substr(e.spent_on, 1, 7) AS month,
       ec.name  AS category,
       ec.color AS category_color,
       trim(c.first_name || ' ' || c.last_name) AS contact_name
  FROM expenses e
  LEFT JOIN expense_categories ec ON ec.id = e.category_id
  LEFT JOIN contacts c ON c.id = e.contact_id;

CREATE VIEW v_monthly_spend AS
SELECT substr(spent_on, 1, 7) AS month,
       COALESCE(ec.name, 'Uncategorized') AS category,
       COUNT(*) AS n,
       SUM(amount_cents) AS total_cents
  FROM expenses e
  LEFT JOIN expense_categories ec ON ec.id = e.category_id
 GROUP BY month, category;

-- seed categories
INSERT INTO expense_categories (name, color, monthly_budget_cents) VALUES
    ('Groceries', '#16a34a', 60000), ('Dining', '#ea580c', 25000), ('Transport', '#2563eb', 20000),
    ('Bills', '#9333ea', 150000), ('Shopping', '#db2777', 20000), ('Health', '#0891b2', NULL),
    ('Entertainment', '#ca8a04', 10000), ('Other', '#6b7280', NULL);
