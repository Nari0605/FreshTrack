
from flask import (
    Flask, request, redirect, render_template,
    url_for, session, g, flash
)
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from datetime import date
import sqlite3
import os
import re

app = Flask(__name__)
app.secret_key = os.environ.get(
    "FLASK_SECRET_KEY",
    "freshtrack-dev-key-change-before-deployment"
)
DB_NAME = "database.db"


# ---------------- DATABASE ----------------

def connect_db():
    db = sqlite3.connect(DB_NAME)
    db.row_factory = sqlite3.Row
    return db


def setup_database():
    with connect_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                price REAL NOT NULL,
                expiry_date TEXT NOT NULL
            )
        """)
        
        db.execute("""
            CREATE TABLE IF NOT EXISTS sales (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                product_id INTEGER,
                product_name TEXT NOT NULL,
                quantity_sold INTEGER NOT NULL,
                unit_price REAL NOT NULL,
                total_amount REAL NOT NULL,
                sold_at TEXT NOT NULL DEFAULT (
                    datetime('now', 'localtime')
                )
            )
        """)


        columns = [
            row["name"]
            for row in db.execute(
                "PRAGMA table_info(products)"
            ).fetchall()
        ]

        # Add ownership to the existing table without deleting products.
        if "user_id" not in columns:
            db.execute(
                "ALTER TABLE products ADD COLUMN user_id INTEGER"
            )

        db.commit()


def get_product(product_id, user_id):
    with connect_db() as db:
        return db.execute("""
            SELECT * FROM products
            WHERE id = ? AND user_id = ?
        """, (product_id, user_id)).fetchone()


# ---------------- LOGIN PROTECTION ----------------

@app.before_request
def load_user():
    g.user = None
    user_id = session.get("user_id")

    if user_id is not None:
        with connect_db() as db:
            g.user = db.execute("""
                SELECT id, name, email, created_at
                FROM users WHERE id = ?
            """, (user_id,)).fetchone()

        if g.user is None:
            session.clear()

    public_endpoints = {"login", "register", "static"}

    if request.endpoint not in public_endpoints and g.user is None:
        return redirect(url_for("login"))


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped_view


# ---------------- INVENTORY DATA ----------------

def get_dashboard_data(user_id, search_query=""):
    with connect_db() as db:
        rows = db.execute("""
            SELECT * FROM products
            WHERE user_id = ?
            ORDER BY expiry_date
        """, (user_id,)).fetchall()

    today = date.today()
    all_items = []

    for row in rows:
        p = dict(row)
        days = (date.fromisoformat(p["expiry_date"]) - today).days

        if days < 0:
            status = "Expired"
        elif days <= 3:
            status = "Near Expiry"
        else:
            status = "Good"

        p["days"] = days
        p["status"] = status
        all_items.append(p)

    total = len(all_items)
    near = sum(p["status"] == "Near Expiry" for p in all_items)
    expired = sum(p["status"] == "Expired" for p in all_items)
    good = sum(p["status"] == "Good" for p in all_items)

    financial_loss = sum(
        p["quantity"] * p["price"]
        for p in all_items if p["status"] == "Expired"
    )

    non_expired_value = sum(
        p["quantity"] * p["price"]
        for p in all_items if p["status"] != "Expired"
    )

    items = [
        p for p in all_items
        if search_query.casefold() in p["name"].casefold()
    ]

    return {
        "all_items": all_items,
        "items": items,
        "total": total,
        "near": near,
        "expired": expired,
        "good": good,
        "financial_loss": financial_loss,
        "non_expired_value": non_expired_value,
        "search_query": search_query
    }


# ---------------- SMART TIPS ----------------

def generate_smart_recommendations(all_items):
    recommendations = []

    groups = [
        {
            "type": "danger",
            "icon": "⚠️",
            "title": "Review expired products",
            "message": (
                "These products have expired. Separate them from "
                "usable stock and assess whether they should be discarded."
            ),
            "products": [
                p for p in all_items if p["status"] == "Expired"
            ]
        },
        {
            "type": "urgent",
            "icon": "⏰",
            "title": "Prioritize these products",
            "message": (
                "These products expire today or tomorrow. Check their "
                "condition and prioritize safe use where appropriate."
            ),
            "products": [
                p for p in all_items
                if p["status"] != "Expired" and p["days"] <= 1
            ]
        },
        {
            "type": "warning",
            "icon": "📅",
            "title": "Plan for near-expiry items",
            "message": (
                "These products expire in 2–3 days. Plan meals or "
                "stock rotation around them."
            ),
            "products": [
                p for p in all_items
                if p["status"] != "Expired" and 1 < p["days"] <= 3
            ]
        },
        {
            "type": "success",
            "icon": "✅",
            "title": "Inventory is looking good",
            "message": "These products have more than 3 days remaining.",
            "products": [
                p for p in all_items if p["status"] == "Good"
            ]
        }
    ]

    for group in groups:
        products = sorted(group["products"], key=lambda p: p["days"])

        if products:
            recommendations.append({
                "type": group["type"],
                "icon": group["icon"],
                "title": group["title"],
                "message": group["message"],
                "count": len(products),
                "value": sum(
                    p["quantity"] * p["price"] for p in products
                ),
                "products": products
            })

    if not all_items:
        recommendations.append({
            "type": "info",
            "icon": "💡",
            "title": "Start by adding products",
            "message": (
                "Add products with quantities, prices and expiry dates "
                "to receive smart recommendations."
            ),
            "count": 0,
            "value": 0,
            "products": []
        })

    return recommendations


# ---------------- REGISTER ----------------

@app.route("/register", methods=["GET", "POST"])
def register():
    if g.user is not None:
        return redirect(url_for("home"))

    error = None

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not name or not email or not password:
            error = "Please fill in every field."
        elif len(name) > 100:
            error = "Name must be 100 characters or fewer."
        elif not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            error = "Please enter a valid email address."
        elif len(email) > 254:
            error = "Email address is too long."
        elif len(password) < 8:
            error = "Password must contain at least 8 characters."
        elif password != confirm_password:
            error = "Passwords do not match."
        else:
            try:
                with connect_db() as db:
                    cursor = db.execute("""
                        INSERT INTO users (name, email, password_hash)
                        VALUES (?, ?, ?)
                    """, (
                        name,
                        email,
                        generate_password_hash(password)
                    ))

                    new_user_id = cursor.lastrowid

                    # Give legacy products to the first account only.
                    db.execute("""
                        UPDATE products
                        SET user_id = ?
                        WHERE user_id IS NULL
                    """, (new_user_id,))

                    db.commit()

                session.clear()
                session["user_id"] = new_user_id
                flash("Your account has been created successfully.")
                return redirect(url_for("home"))

            except sqlite3.IntegrityError:
                error = "An account with this email already exists."

    return render_template("register.html", error=error)


# ---------------- LOGIN ----------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user is not None:
        return redirect(url_for("home"))

    error = None

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        with connect_db() as db:
            user = db.execute("""
                SELECT * FROM users WHERE email = ?
            """, (email,)).fetchone()

        if user is None or not check_password_hash(
            user["password_hash"], password
        ):
            error = "Incorrect email or password."
        else:
            session.clear()
            session["user_id"] = user["id"]
            return redirect(url_for("home"))

    return render_template("login.html", error=error)


# ---------------- LOGOUT ----------------

@app.route("/logout", methods=["POST"])
@login_required
def logout():
    session.clear()
    flash("You have logged out successfully.")
    return redirect(url_for("login"))


# ---------------- PROFILE ----------------

@app.route("/profile")
@login_required
def profile():
    return render_template("profile.html", user=g.user)


# ---------------- OVERVIEW ----------------

@app.route("/")
@login_required
def home():
    return render_template(
        "dashboard.html",
        page="overview",
        user=g.user,
        **get_dashboard_data(g.user["id"])
    )


# ---------------- ADD PRODUCT ----------------

@app.route("/add", methods=["GET", "POST"])
@login_required
def add_product():
    error = None

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        expiry = request.form.get("expiry_date", "")

        try:
            quantity = int(request.form.get("quantity", ""))
            price = float(request.form.get("price", ""))
            date.fromisoformat(expiry)

            if not name or quantity < 0 or price < 0:
                raise ValueError

        except (ValueError, TypeError):
            error = "Please enter valid product details."
        else:
            with connect_db() as db:
                db.execute("""
                    INSERT INTO products
                    (name, quantity, price, expiry_date, user_id)
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    name, quantity, price, expiry, g.user["id"]
                ))
                db.commit()

            return redirect(url_for("inventory"))

    return render_template(
        "dashboard.html",
        page="add",
        user=g.user,
        error=error,
        **get_dashboard_data(g.user["id"])
    )


# ---------------- INVENTORY / SEARCH ----------------

@app.route("/inventory")
@login_required
def inventory():
    search_query = request.args.get("search", "").strip()

    return render_template(
        "dashboard.html",
        page="inventory",
        user=g.user,
        **get_dashboard_data(g.user["id"], search_query)
    )


# ---------------- ANALYTICS ----------------

@app.route("/analytics")
@login_required
def analytics():
    return render_template(
        "dashboard.html",
        page="analytics",
        user=g.user,
        **get_dashboard_data(g.user["id"])
    )


# ---------------- EXPIRY ALERTS ----------------

@app.route("/alerts")
@login_required
def alerts():
    return render_template(
        "dashboard.html",
        page="alerts",
        user=g.user,
        **get_dashboard_data(g.user["id"])
    )


# ---------------- SMART TIPS ----------------

@app.route("/recommendations")
@login_required
def recommendations():
    data = get_dashboard_data(g.user["id"])
    smart_tips = generate_smart_recommendations(data["all_items"])

    return render_template(
        "dashboard.html",
        page="recommendations",
        user=g.user,
        recommendations=smart_tips,
        **data
    )


# ---------------- EDIT PRODUCT ----------------

@app.route("/edit/<int:product_id>", methods=["GET", "POST"])
@login_required
def edit_product(product_id):
    product = get_product(product_id, g.user["id"])

    if product is None:
        return "Product not found.", 404

    error = None

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        expiry = request.form.get("expiry_date", "")

        try:
            quantity = int(request.form.get("quantity", ""))
            price = float(request.form.get("price", ""))
            date.fromisoformat(expiry)

            if not name or quantity < 0 or price < 0:
                raise ValueError

        except (ValueError, TypeError):
            error = "Please enter valid product details."
        else:
            with connect_db() as db:
                db.execute("""
                    UPDATE products
                    SET name = ?, quantity = ?, price = ?, expiry_date = ?
                    WHERE id = ? AND user_id = ?
                """, (
                    name, quantity, price, expiry,
                    product_id, g.user["id"]
                ))
                db.commit()

            return redirect(url_for("inventory"))

    return render_template(
        "edit_product.html",
        product=product,
        error=error,
        user=g.user
    )


# ---------------- DELETE PRODUCT ----------------



@app.route("/sell/<int:product_id>", methods=["POST"])
@login_required
def sell_product(product_id):
    try:
        quantity_sold = int(request.form.get("quantity_sold", ""))
    except (ValueError, TypeError):
        flash("Please enter a valid quantity.", "error")
        return redirect(url_for("inventory"))

    if quantity_sold <= 0:
        flash("Quantity sold must be greater than zero.", "error")
        return redirect(url_for("inventory"))

    with connect_db() as db:
        try:
            # Check that this product belongs to the logged-in user.
            product = db.execute(
                """SELECT * FROM products
                   WHERE id = ? AND user_id = ?""",
                (product_id, g.user["id"])
            ).fetchone()

            if product is None:
                flash("Product not found.", "error")

            elif quantity_sold > product["quantity"]:
                flash("You cannot sell more than the available stock.", "error")

            else:
                unit_price = float(product["price"])
                total_amount = round(quantity_sold * unit_price, 2)

                # Reduce stock only if sufficient quantity is available.
                cursor = db.execute(
                    """UPDATE products
                       SET quantity = quantity - ?
                       WHERE id = ? AND user_id = ? AND quantity >= ?""",
                    (quantity_sold, product_id, g.user["id"], quantity_sold)
                )

                if cursor.rowcount != 1:
                    raise ValueError("Stock changed. Please try again.")

                # Record the sale in the same database transaction.
                db.execute(
                    """INSERT INTO sales
                       (user_id, product_id, product_name,
                        quantity_sold, unit_price, total_amount)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        g.user["id"],
                        product_id,
                        product["name"],
                        quantity_sold,
                        unit_price,
                        total_amount
                    )
                )

                flash(
                    f"Sale recorded! {quantity_sold} unit(s) of "
                    f"{product['name']} sold for ₹{total_amount:.2f}. "
                    f"Remaining stock: {product['quantity'] - quantity_sold}.",
                    "success"
                )

        except ValueError as exc:
            db.rollback()
            flash(str(exc), "error")

    return redirect(url_for("inventory"))


@app.route("/delete/<int:product_id>", methods=["POST"])
@login_required
def delete_product(product_id):
    with connect_db() as db:
        db.execute("""
            DELETE FROM products
            WHERE id = ? AND user_id = ?
        """, (product_id, g.user["id"]))
        db.commit()

    return redirect(url_for("inventory"))


@app.route("/sales-history")
@login_required
def sales_history():
    with connect_db() as db:
        sales = db.execute(
            """
            SELECT product_name, quantity_sold, unit_price,
                   total_amount, sold_at
            FROM sales
            WHERE user_id = ?
            ORDER BY sold_at DESC, id DESC
            """,
            (g.user["id"],)
        ).fetchall()

        summary = db.execute(
            """
            SELECT
                COUNT(*) AS total_transactions,
                COALESCE(SUM(quantity_sold), 0) AS total_units,
                COALESCE(SUM(total_amount), 0) AS total_revenue
            FROM sales
            WHERE user_id = ?
            """,
            (g.user["id"],)
        ).fetchone()

    return render_template(
        "sales_history.html",
        user=g.user,
        sales=sales,
        summary=summary
    )

setup_database()

if __name__ == "__main__":
    app.run(debug=True)
