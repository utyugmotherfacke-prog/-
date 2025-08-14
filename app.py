import os
import csv
import sqlite3
from datetime import date, datetime, timedelta
from contextlib import closing
from flask import Flask, render_template, request, redirect, url_for, send_file, flash


DATABASE_PATH = os.path.join(os.path.dirname(__file__), 'finance.db')

app = Flask(__name__)
app.secret_key = os.environ.get('APP_SECRET_KEY', 'dev-secret-key')


def get_db_connection() -> sqlite3.Connection:
	conn = sqlite3.connect(DATABASE_PATH, detect_types=sqlite3.PARSE_DECLTYPES, check_same_thread=False)
	conn.row_factory = sqlite3.Row
	conn.execute('PRAGMA foreign_keys = ON;')
	return conn


def init_db() -> None:
	with closing(get_db_connection()) as conn:
		conn.executescript(
			'''
			CREATE TABLE IF NOT EXISTS categories (
				id INTEGER PRIMARY KEY AUTOINCREMENT,
				name TEXT NOT NULL UNIQUE,
				type TEXT NOT NULL CHECK (type IN ('income','expense'))
			);

			CREATE TABLE IF NOT EXISTS transactions (
				id INTEGER PRIMARY KEY AUTOINCREMENT,
				date TEXT NOT NULL,
				type TEXT NOT NULL CHECK (type IN ('income','expense')),
				category_id INTEGER NOT NULL,
				amount REAL NOT NULL CHECK (amount >= 0),
				description TEXT,
				FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE RESTRICT ON UPDATE CASCADE
			);
			'''
		)
		# seed default categories if empty
		cur = conn.execute('SELECT COUNT(*) AS cnt FROM categories')
		count = cur.fetchone()['cnt']
		if count == 0:
			default_categories = [
				('Зарплата', 'income'),
				('Фриланс', 'income'),
				('Питание', 'expense'),
				('Транспорт', 'expense'),
				('Коммунальные услуги', 'expense'),
				('Развлечения', 'expense'),
			]
			conn.executemany('INSERT INTO categories (name, type) VALUES (?, ?)', default_categories)
		conn.commit()


def parse_date(value: str) -> date:
	return datetime.strptime(value, '%Y-%m-%d').date()


def month_bounds(target_date: date) -> tuple[str, str]:
	first = target_date.replace(day=1)
	if first.month == 12:
		next_month_first = first.replace(year=first.year + 1, month=1)
	else:
		next_month_first = first.replace(month=first.month + 1)
	last = next_month_first - timedelta(days=1)
	return first.strftime('%Y-%m-%d'), last.strftime('%Y-%m-%d')


@app.route('/')
def index():
	init_db()
	today = date.today()
	month_start, month_end = month_bounds(today)
	with closing(get_db_connection()) as conn:
		income_sum = conn.execute(
			"SELECT COALESCE(SUM(amount),0) AS total FROM transactions WHERE type='income' AND date BETWEEN ? AND ?",
			(month_start, month_end),
		).fetchone()['total']
		expense_sum = conn.execute(
			"SELECT COALESCE(SUM(amount),0) AS total FROM transactions WHERE type='expense' AND date BETWEEN ? AND ?",
			(month_start, month_end),
		).fetchone()['total']
		balance_all_time = conn.execute(
			"SELECT COALESCE((SELECT SUM(amount) FROM transactions WHERE type='income'),0) - COALESCE((SELECT SUM(amount) FROM transactions WHERE type='expense'),0) AS bal"
		).fetchone()['bal']
		latest = conn.execute(
			'''SELECT t.id, t.date, t.type, t.amount, t.description, c.name AS category_name
			   FROM transactions t JOIN categories c ON c.id = t.category_id
			   ORDER BY t.date DESC, t.id DESC LIMIT 10'''
		).fetchall()
		income_categories = conn.execute("SELECT id, name FROM categories WHERE type='income' ORDER BY name").fetchall()
		expense_categories = conn.execute("SELECT id, name FROM categories WHERE type='expense' ORDER BY name").fetchall()
	return render_template(
		'index.html',
		month_income=income_sum,
		month_expense=expense_sum,
		balance=balance_all_time,
		latest=latest,
		income_categories=income_categories,
		expense_categories=expense_categories,
		today=today.strftime('%Y-%m-%d'),
	)


@app.route('/transactions')
def transactions():
	init_db()
	type_filter = request.args.get('type')
	category_id = request.args.get('category_id')
	from_date = request.args.get('from')
	to_date = request.args.get('to')

	query = [
		"SELECT t.id, t.date, t.type, t.amount, t.description, c.name AS category_name",
		"FROM transactions t JOIN categories c ON c.id = t.category_id",
		"WHERE 1=1",
	]
	params: list = []
	if type_filter in ('income', 'expense'):
		query.append("AND t.type = ?")
		params.append(type_filter)
	if category_id and category_id.isdigit():
		query.append("AND t.category_id = ?")
		params.append(int(category_id))
	if from_date:
		query.append("AND t.date >= ?")
		params.append(from_date)
	if to_date:
		query.append("AND t.date <= ?")
		params.append(to_date)
	query.append("ORDER BY t.date DESC, t.id DESC")

	with closing(get_db_connection()) as conn:
		rows = conn.execute("\n".join(query), params).fetchall()
		categories = conn.execute("SELECT id, name, type FROM categories ORDER BY type, name").fetchall()
	return render_template('transactions.html', transactions=rows, categories=categories)


@app.route('/transaction', methods=['POST'])
def add_transaction():
	init_db()
	try:
		trx_type = request.form['type']
		category_id = int(request.form['category_id'])
		amount = float(request.form['amount'])
		trx_date = request.form.get('date') or date.today().strftime('%Y-%m-%d')
		description = request.form.get('description') or ''
		if trx_type not in ('income', 'expense'):
			raise ValueError('Некорректный тип операции')
		if amount < 0:
			raise ValueError('Сумма не может быть отрицательной')
		# Ensure date is valid
		parse_date(trx_date)
		with closing(get_db_connection()) as conn:
			conn.execute(
				'INSERT INTO transactions (date, type, category_id, amount, description) VALUES (?, ?, ?, ?, ?)',
				(trx_date, trx_type, category_id, amount, description),
			)
			conn.commit()
		flash('Операция добавлена', 'success')
	except Exception as ex:
		flash(f'Ошибка: {ex}', 'error')
	return redirect(url_for('index'))


@app.route('/transaction/delete/<int:trx_id>', methods=['POST'])
def delete_transaction(trx_id: int):
	init_db()
	with closing(get_db_connection()) as conn:
		conn.execute('DELETE FROM transactions WHERE id = ?', (trx_id,))
		conn.commit()
	flash('Операция удалена', 'success')
	return redirect(request.referrer or url_for('transactions'))


@app.route('/categories', methods=['GET', 'POST'])
def categories():
	init_db()
	if request.method == 'POST':
		name = (request.form.get('name') or '').strip()
		type_value = request.form.get('type')
		if not name:
			flash('Укажите название категории', 'error')
		elif type_value not in ('income', 'expense'):
			flash('Некорректный тип категории', 'error')
		else:
			try:
				with closing(get_db_connection()) as conn:
					conn.execute('INSERT INTO categories (name, type) VALUES (?, ?)', (name, type_value))
					conn.commit()
				flash('Категория добавлена', 'success')
			except sqlite3.IntegrityError:
				flash('Такая категория уже существует', 'error')
	return _render_categories()


def _render_categories():
	with closing(get_db_connection()) as conn:
		income = conn.execute("SELECT id, name FROM categories WHERE type='income' ORDER BY name").fetchall()
		expense = conn.execute("SELECT id, name FROM categories WHERE type='expense' ORDER BY name").fetchall()
	return render_template('categories.html', income_categories=income, expense_categories=expense)


@app.route('/report')
def report():
	init_db()
	# Monthly totals by category for the current month
	today = date.today()
	start, end = month_bounds(today)
	with closing(get_db_connection()) as conn:
		by_category = conn.execute(
			'''
			SELECT c.name AS category_name,
			       SUM(CASE WHEN t.type='income' THEN t.amount ELSE 0 END) AS income_total,
			       SUM(CASE WHEN t.type='expense' THEN t.amount ELSE 0 END) AS expense_total
			FROM categories c
			LEFT JOIN transactions t ON t.category_id = c.id AND t.date BETWEEN ? AND ?
			GROUP BY c.id
			ORDER BY c.type, c.name
			''',
			(start, end),
		).fetchall()
		monthly_income = conn.execute(
			"SELECT COALESCE(SUM(amount),0) FROM transactions WHERE type='income' AND date BETWEEN ? AND ?",
			(start, end),
		).fetchone()[0]
		monthly_expense = conn.execute(
			"SELECT COALESCE(SUM(amount),0) FROM transactions WHERE type='expense' AND date BETWEEN ? AND ?",
			(start, end),
		).fetchone()[0]
	return render_template(
		'report.html',
		rows=by_category,
		start=start,
		end=end,
		monthly_income=monthly_income,
		monthly_expense=monthly_expense,
	)


@app.route('/export.csv')
def export_csv():
	init_db()
	csv_path = os.path.join(os.path.dirname(__file__), 'transactions_export.csv')
	with closing(get_db_connection()) as conn:
		rows = conn.execute(
			'''SELECT t.id, t.date, t.type, c.name AS category, t.amount, COALESCE(t.description,'') AS description
			   FROM transactions t JOIN categories c ON c.id = t.category_id
			   ORDER BY t.date ASC, t.id ASC'''
		).fetchall()
		with open(csv_path, 'w', newline='', encoding='utf-8') as f:
			writer = csv.writer(f)
			writer.writerow(['id', 'date', 'type', 'category', 'amount', 'description'])
			for r in rows:
				writer.writerow([r['id'], r['date'], r['type'], r['category'], f"{r['amount']:.2f}", r['description']])
	return send_file(csv_path, as_attachment=True, download_name='transactions.csv')


if __name__ == '__main__':
	init_db()
	app.run(host='0.0.0.0', port=8000, debug=True)