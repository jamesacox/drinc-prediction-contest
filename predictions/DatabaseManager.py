from apscheduler.scheduler import Scheduler
from decimal import Decimal
import csv
import datetime
import pycassa
import simplejson as json
import time
import urllib2
import uuid

# Database access.
pool = pycassa.ConnectionPool('PredictionContest', server_list=['flash:9160'])
transactions_by_user_col = pycassa.ColumnFamily(pool, 'TransactionsByUser')
transactions_by_stock_col = pycassa.ColumnFamily(pool, 'TransactionsByStock')
transactions_col = pycassa.ColumnFamily(pool, 'Transactions')
user_history_col = pycassa.ColumnFamily(pool, 'UserHist')
stock_history_col = pycassa.ColumnFamily(pool, 'StockHist')
stocks_col = pycassa.ColumnFamily(pool, 'Stocks')

class DatabaseManager(object):
    def __init__(self, members, tickers):
        self.members = members
        self.tickers = tickers
        self.sched = Scheduler()

    def start(self):
        self.sched.start()
        self.sched.add_cron_job(self.update_stock_histories, day_of_week='0-4', hour=9)
        self.sched.add_cron_job(self.update_user_histories, day_of_week='0-4', hour=18)
        self.sched.add_cron_job(self.update_stock_prices, day_of_week='0-4', hour='8-17', minute='0,15,30,45')

    def get_stock_expenditure(self, ticker):
        """Figure out how much was spent on a given stock"""
        try:
            transactions = transactions_by_stock_col.get(ticker)
            spent = sum([int(x) for x in transactions.values()])
        except:
            spent = 0

        return spent

    def get_user_transactions(self, user):
        """Get the transactions associated with a user"""
        try:
            transaction_ids = transactions_by_user_col.get(user)
            transactions = [transactions_col.get(tid) for tid in transaction_ids]
        except:
            transactions = []
        return transactions

    def get_current_value(self, transaction):
        """Gets the current value of a transaction, in pennies"""
        try:
            stock = stocks_col.get(transaction['stock'])
            cost = transaction['cost']
            value = (cost * stock['price']) / transaction['price']
        except:
            value = 0
        return int(value)

    def get_value_at(self, transaction, date):
        """Gets the value of a transaction on a particular date, in pennies"""
        cost = transaction['cost']
        stock = transaction['stock']
        purchase_price = transaction['price']
        if transaction['date'] < date:
            try:
                history = stock_history_col.get(stock, column_start=date, column_count=1, column_reversed=True)
                for (date, price) in history.items():
                    value = (cost * price) / purchase_price
            except:
                value = 0
        else:
            value = 0

        return int(value)

    def get_current_user_value(self, member):
        """Get a user's current value, in pennies"""
        try:
            tids = transactions_by_user_col.get(member)
        except:
            tids = {}

        total = 0
        for tid in tids:
            transaction = transactions_col.get(tid)
            total = total + self.get_current_value(transaction)

        return total

    def get_user_value_at(self, member, date):
        """Get a user's current value on a particular date, in pennies"""
        try:
            tids = transactions_by_user_col.get(member)
        except:
            tids = {}

        total = 0
        for tid in tids:
            transaction = transactions_col.get(tid)
            total = total + self.get_value_at(transaction, date)

        return total

    def record_purchase(self, user, stock, price, cost):
        """Updates the database with a record of a purchase."""
        transaction = {'user':user,
                       'stock':stock,
                       'date':datetime.datetime.utcnow(),
                       'price':price,
                       'cost':cost}
        transaction_id = uuid.uuid4()

        transactions_col.insert(transaction_id, transaction)
        transactions_by_user_col.insert(user, {transaction_id:stock})
        transactions_by_stock_col.insert(stock, {transaction_id:cost})

    def get_user_history(self, user, start_date):
        """Get the historical value of a user's portfolio"""
        try:
            history = user_history_col.get(user, column_start=start_date)
        except:
            history = {}

        return history

    def get_stock_history_from_google(self, ticker):
        """Goes to the internet, and returns a dictionary in which the keys are dates,
        and the values are prices"""
        url = 'http://www.google.com/finance/historical?q=%s&output=csv' % ticker
        data = urllib2.urlopen(url).read()

        reader = csv.reader(data.split())
        reader.next()

        dict = {}
        for row in reader:
            (date, closing) = (row[0], row[4])
            struct = time.strptime(date, '%d-%b-%y')
            dt = datetime.datetime(*struct[:6])
            price = Decimal(closing)
            dict[dt] = price

        return dict

    def get_stock_price(self, ticker):
        """Gets a stock price, first trying the database and if that fails then
        going to Google and updating the database with the result"""
        price = self.get_stock_price_from_db(ticker)
        if not price:
            price = self.update_stock_price(ticker)
        return price

    def get_stock_price_from_db(self, ticker):
        """Gets a recent stock price from the database"""
        try:
            price = stocks_col.get(ticker)['price']
        except:
            price = None

        return price

    def get_stock_price_from_google(self, ticker):
        """Returns the latest stock price from Google Finance"""
        url = 'http://finance.google.com/finance/info?q=%s' % ticker
        try:
            lines = urllib2.urlopen(url).read().splitlines()
            quote = json.loads(''.join([x for x in lines if x not in ('// [', ']')]))
            price = quote['l_cur'].replace(',','')
            price = Decimal(price[3:])
        except:
            price = None

        return price

    def update_stock_histories(self):
        """Update the StockHist column family"""
        for stock in self.tickers:
            dict = self.get_stock_history_from_google(stock)
            stock_history_col.insert(stock, dict)

    def update_user_histories(self):
        """Update the UserHist column family"""
        today = datetime.date.today()
        timestamp = datetime.datetime.combine(today, datetime.time())
        for member in self.members:
            worth = self.get_current_user_value(member)
            user_history_col.insert(member, {timestamp: worth})

    def update_stock_prices(self):
        """Update the Stocks column family with all the latest prices"""
        for ticker in self.tickers:
            self.update_stock_price(ticker)

    def update_stock_price(self, ticker):
        """Update the Stocks column family with the latest price for a stock"""
        price = self.get_stock_price_from_google(ticker)
        if price:
            stocks_col.insert(ticker, {'price':price})
        return price
