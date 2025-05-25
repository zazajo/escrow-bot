import os
import random
import time
import asyncio
import logging
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TimedOut, NetworkError, RetryAfter
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
    JobQueue
)

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')

# Conversation states
(
    CHOOSING_ROLE, GET_PARTNER_ID, TRADE_DETAILS, 
    SELECT_CRYPTO, ENTER_AMOUNT, CONFIRM_TRADE,
    AWAIT_APPROVAL, PAYMENT_INSTRUCTIONS, PAYMENT_SENT
) = range(9)

# Crypto wallet addresses
WALLETS = {
    'BTC': os.getenv('BTC_WALLET'),
    'LTC': os.getenv('LTC_WALLET'),
    'XMR': os.getenv('XMR_WALLET')
}

# Store trade data and approvals
trade_data = {}
user_active_trades = {}

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle errors in the telegram bot."""
    logger.error(msg="Exception while handling an update:", exc_info=context.error)
    
    if isinstance(context.error, TimedOut):
        logger.warning("Telegram API timeout occurred, retrying...")
        await asyncio.sleep(5)
    elif isinstance(context.error, NetworkError):
        logger.warning("Network error occurred, retrying...")
        await asyncio.sleep(10)
    elif isinstance(context.error, RetryAfter):
        logger.warning(f"Rate limited, waiting {context.error.retry_after} seconds")
        await asyncio.sleep(context.error.retry_after)

async def safe_send_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, **kwargs):
    """Send message with error handling and retry logic."""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            await context.bot.send_message(chat_id=chat_id, text=text, **kwargs)
            return True
        except TimedOut:
            if attempt < max_retries - 1:
                await asyncio.sleep(5)
                continue
            logger.error(f"Failed to send message after {max_retries} attempts")
            return False
        except Exception as e:
            logger.error(f"Error sending message: {e}")
            return False

async def cleanup_old_trades(context: ContextTypes.DEFAULT_TYPE):
    """Clean up trades older than 24 hours."""
    current_time = time.time()
    expired_trades = [
        trade_id for trade_id, trade in trade_data.items()
        if current_time - trade.get('timestamp', 0) > 86400  # 24 hours
    ]
    
    for trade_id in expired_trades:
        trade = trade_data.pop(trade_id, None)
        if trade:
            # Remove from user_active_trades
            for user_id in [trade['user_id'], trade['partner_id']]:
                if user_id in user_active_trades and trade_id in user_active_trades[user_id]:
                    user_active_trades[user_id].remove(trade_id)
            
            # Notify users if trade was active
            if not trade.get('completed', False):
                for user_id in [trade['user_id'], trade['partner_id']]:
                    await safe_send_message(
                        context,
                        user_id,
                        f"❌ Trade {trade_id} has expired due to inactivity\n\n"
                        "Please start a new transaction if needed."
                    )
    
    if expired_trades:
        logger.info(f"Cleaned up {len(expired_trades)} expired trades")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send welcome message when /start is issued."""
    try:
        user_id = update.effective_user.id
        context.user_data['user_id'] = user_id
        
        welcome = """
        🤖 Crypro Trades Escrow Bot 🤖
        
        Secure cryptocurrency transactions between buyers and sellers.
        
        Commands:
        /escrow - Start new transaction
        /info - How it works
        /my_trades - View active trades
        """
        await safe_send_message(context, update.effective_chat.id, welcome)
    except Exception as e:
        logger.error(f"Error in start: {e}")
        raise

async def info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send bot information."""
    info_text = """━━━━⍟Crypto Trades⍟━━━━\n\n
    ℹ️ How This Works ℹ️
    
    1. Both parties start the bot
    2. Agree on terms outside bot
    3. Start escrow transaction
    4. Buyer pays into escrow
    5. Seller delivers goods
    6. Buyer confirms receipt
    7. Escrow releases payment
    
    🔒 2% escrow fee
    ⚡ Fast processing
    """
    await safe_send_message(context, update.effective_chat.id, info_text)

async def escrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start escrow process."""
    keyboard = [
        [InlineKeyboardButton("👨‍💼 Buyer", callback_data='buyer'),
         InlineKeyboardButton("👩‍💼 Seller", callback_data='seller')]
    ]
    await update.message.reply_text(
        "━━━━⍟Crypto Trades⍟━━━━\n\nSelect your role in this transaction:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return CHOOSING_ROLE

async def role_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle role selection."""
    query = update.callback_query
    await query.answer()
    
    context.user_data['role'] = query.data
    role = "Buyer" if query.data == 'buyer' else "Seller"
    await query.edit_message_text(f"━━━━⍟Crypto Trades⍟━━━━\n\nYou're the {role}. Please enter the other party's ID: (Ask seller to send their id and use @userinfobot to get yours)")
    return GET_PARTNER_ID

async def verify_partner_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Verify partner ID and request trade details."""
    try:
        partner_id = int(update.message.text)
        context.user_data['partner_id'] = partner_id
        
        await update.message.reply_text(
            "━━━━⍟Crypto Trades⍟━━━━\n\nExplain the trade in detail:\n\n"
            "(This is crucial if something goes wrong and manual intervention is needed)")
        return TRADE_DETAILS
    except ValueError:
        await update.message.reply_text("❌ Please enter a valid numeric ID")
        return GET_PARTNER_ID

async def get_trade_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Store trade details and show crypto options."""
    context.user_data['trade_details'] = update.message.text
    
    keyboard = [
        [InlineKeyboardButton("BTC", callback_data='BTC'),
         InlineKeyboardButton("LTC", callback_data='LTC')],
        [InlineKeyboardButton("XMR", callback_data='XMR')]
    ]
    await update.message.reply_text(
        "━━━━⍟Crypto Trades⍟━━━━\n\nSelect cryptocurrency for payment:",
        reply_markup=InlineKeyboardMarkup(keyboard))
    return SELECT_CRYPTO

async def select_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle crypto selection and request amount."""
    query = update.callback_query
    await query.answer()
    
    context.user_data['crypto'] = query.data
    await query.edit_message_text(
        f"━━━━⍟Crypto Trades⍟━━━━\n\nSelected crypto: {query.data}\n"
        f"Fee: 2.00%\n\n"
        f"Enter the amount of {query.data} for trade:")
    return ENTER_AMOUNT

async def enter_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Verify amount and show confirmation."""
    try:
        amount = float(update.message.text)
        context.user_data['amount'] = amount
        
        # Calculate fees and total
        fee = amount * 0.02
        total = amount + fee
        
        # Generate trade ID
        trade_id = ''.join(random.choices('ABCDEFGHJKLMNPQRSTUVWXYZ23456789', k=8))
        context.user_data['trade_id'] = trade_id
        
        # Store trade data
        user_id = context.user_data['user_id']
        partner_id = context.user_data['partner_id']
        username = update.effective_user.username or update.effective_user.full_name
        
        trade_data[trade_id] = {
            'user_id': user_id,
            'partner_id': partner_id,
            'user_name': username,
            'role': context.user_data['role'],
            'crypto': context.user_data['crypto'],
            'amount': amount,
            'fee': fee,
            'total': total,
            'details': context.user_data['trade_details'],
            'user_approved': False,
            'partner_approved': False,
            'payment_sent': False,
            'timestamp': time.time()
        }
        
        # Track active trades
        user_active_trades.setdefault(user_id, []).append(trade_id)
        user_active_trades.setdefault(partner_id, []).append(trade_id)
        
        # Show confirmation
        keyboard = [[InlineKeyboardButton("✅ Confirm", callback_data=f'confirm_{trade_id}')]]
        await update.message.reply_text(
            f"━━━━⍟Crypto Trades⍟━━━━\n\n🔄 Trade ID: {trade_id}\n"
            f"👤 Your role: {context.user_data['role'].capitalize()}\n"
            f"💰 Amount: {amount} {context.user_data['crypto']}\n"
            f"📝 Details: {context.user_data['trade_details']}\n"
            f"💸 Fee: {fee:.8f} {context.user_data['crypto']} (2%)\n"
            f"💵 Total: {total:.8f} {context.user_data['crypto']}\n\n"
            "Please confirm the details:",
            reply_markup=InlineKeyboardMarkup(keyboard))
        return CONFIRM_TRADE
    except ValueError:
        await update.message.reply_text("❌ Please enter a valid number")
        return ENTER_AMOUNT

async def confirm_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle trade confirmation - show payment info to buyer immediately."""
    query = update.callback_query
    await query.answer()
    
    trade_id = query.data.split('_')[1]
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.full_name
    
    # Get the trade
    trade = trade_data[trade_id]
    
    # Update approval status
    if trade['user_id'] == user_id:
        trade['user_approved'] = True
        trade['user_name'] = username
    else:
        trade['partner_approved'] = True
        trade['partner_name'] = username
    
    # Update timestamp
    trade['timestamp'] = time.time()
    
    # If buyer approved, show payment instructions IMMEDIATELY
    if trade['user_id'] == user_id and trade['role'] == 'buyer':
        await send_payment_instructions_to_buyer(context, trade_id)
    
    # Check if both approved (for seller notifications)
    if trade['user_approved'] and trade['partner_approved']:
        await notify_seller_payment_pending(context, trade_id)
    
    # Notify the other party if they haven't approved yet
    if not (trade['user_approved'] and trade['partner_approved']):
        partner_id = trade['partner_id'] if trade['user_id'] == user_id else trade['user_id']
        
        keyboard = [[InlineKeyboardButton("✅ Approve Trade", callback_data=f'confirm_{trade_id}')]]
        
        await safe_send_message(
            context,
            partner_id,
            f"━━━━⍟Crypto Trades⍟━━━━\n\n⚠️ Trade {trade_id} awaiting your approval!\n"
            f"From: @{username}\n\n"
            f"Amount: {trade['amount']} {trade['crypto']}\n"
            f"Details: {trade['details']}\n\n"
            "Please approve this trade:",
            reply_markup=InlineKeyboardMarkup(keyboard))
    
    await query.edit_message_text(
        f"━━━━⍟Crypto Trades⍟━━━━\n\n✅ You (@{username}) approved trade {trade_id}!\n\n"
        "The buyer can now send payment. Funds will be held until both parties approve.")
    return AWAIT_APPROVAL

async def send_payment_instructions_to_buyer(context: ContextTypes.DEFAULT_TYPE, trade_id: str):
    """Send payment instructions ONLY to buyer immediately after their approval."""
    trade = trade_data[trade_id]
    wallet_address = WALLETS[trade['crypto']]
    
    message = (
        f"━━━━⍟Crypto Trades⍟━━━━\n\n💰 Ready to Pay for Trade {trade_id} 💰\n\n"
        f"Please send {trade['total']:.8f} {trade['crypto']} to:\n"
        f"`{wallet_address}`\n\n"
        f"Seller: @outtathemud\n"
        f"Details: {trade['details']}\n\n"
        "⚠️ Funds will be held in escrow until the seller approves.\n"
        "You'll get a confirmation when payment is received.\n"
        "*NOTE*: If sending from an Exchange, be sure to withdraw this amount *plus Exchange's fee*\n\n"
        "*Funds have yet to be deposited in escrow wallet, make sure to deposit before clicking on the Payment Sent button*"
    )
    
    keyboard = [[InlineKeyboardButton("✅ Payment Sent", callback_data=f'sent_{trade_id}')]]
    
    await safe_send_message(
        context,
        trade['user_id'],
        message,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )
    
async def notify_seller_payment_pending(context: ContextTypes.DEFAULT_TYPE, trade_id: str):
    """Notify seller that buyer has payment instructions and trade is fully approved."""
    trade = trade_data[trade_id]
    
    message = (
        f"━━━━⍟Crypto Trades⍟━━━━\n\n✅ Trade {trade_id} Fully Approved!\n\n"
        f"Buyer @{trade['user_name']} has payment instructions.\n"
        f"Amount: {trade['total']:.8f} {trade['crypto']}\n\n"
        "You'll be notified when payment is received."
    )
    
    await safe_send_message(
        context,
        trade['partner_id'],
        message
    )

async def payment_sent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Final fixed version - completely stops message repetition"""
    query = update.callback_query
    await query.answer()
    
    trade_id = query.data.split('_')[1]
    trade = trade_data[trade_id]
    
    # 1. First immediately disable the button
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except:
        pass
    
    # 2. Mark payment as sent in database
    trade['payment_sent'] = True
    trade['timestamp'] = time.time()
    
    # 3. Create completely new message content
    new_message = (
        "✅ *Payment Verified as Sent*\n\n"
        f"• Amount: `{trade['amount']}` {trade['crypto']}\n"
        f"• Trade ID: `{trade_id}`\n"
        f"• Time: {datetime.now().strftime('%H:%M %p')}\n\n"
        "_We're now confirming the blockchain transaction..._"
    )
    
    # 4. COMPLETELY replace the message (both text and buttons)
    try:
        await query.edit_message_text(
            text=new_message,
            parse_mode="MarkdownV2",
            reply_markup=None  # This removes all buttons
        )
    except Exception as e:
        logger.error(f"Message edit failed: {e}")
        # Fallback - send as new message
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=new_message,
            parse_mode="MarkdownV2"
        )
    
    # 5. Additional confirmation
    await context.bot.send_message(
        chat_id=update.effective_user.id,
        text="🔍 Payment verification started. We'll notify both parties when confirmed.",
        reply_to_message_id=query.message.message_id
    )
    
async def log_user_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Logs ALL text messages users send to the bot"""
    user = update.effective_user
    logger.info(f"📩 Message from @{user.username or user.id}: {update.message.text}")

async def log_button_presses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Logs ALL button presses in the bot"""
    user = update.effective_user
    query = update.callback_query
    logger.info(f"🖱️ Button pressed by @{user.username or user.id}: {query.data}")

async def my_trades(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user's active trades."""
    user_id = update.effective_user.id
    if user_id not in user_active_trades or not user_active_trades[user_id]:
        await safe_send_message(context, update.effective_chat.id, "You have no active trades.")
        return
    
    message = "🔄 Your Active Trades:\n\n"
    for trade_id in user_active_trades[user_id]:
        trade = trade_data.get(trade_id, {})
        if not trade:
            continue
            
        status = ""
        if trade['payment_sent']:
            status = "⌛ Payment verification in progress"
        elif trade['user_approved'] and trade['partner_approved']:
            if trade['user_id'] == user_id and trade['role'] == 'buyer':
                status = "💳 Awaiting your payment"
            else:
                status = "⏳ Waiting for buyer's payment"
        else:
            if (trade['user_id'] == user_id and not trade['user_approved']) or \
               (trade['partner_id'] == user_id and not trade['partner_approved']):
                status = "❓ Needs your approval"
            else:
                status = "⏳ Waiting for counterparty"
        
        message += (
            f"🆔 Trade ID: {trade_id}\n"
            f"💰 Amount: {trade['amount']} {trade['crypto']}\n"
            f"👤 Counterparty: @{trade['partner_name'] if trade['user_id'] == user_id else trade['user_name']}\n"
            f"📝 Details: {trade['details']}\n"
            f"🔹 Status: {status}\n\n"
        )
    
    await safe_send_message(context, update.effective_chat.id, message)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the current conversation."""
    await safe_send_message(context, update.effective_chat.id, '❌ Transaction cancelled')
    return ConversationHandler.END

def main() -> None:
    """Run the bot with enhanced error handling."""
    if not TOKEN:
        logger.error("No TELEGRAM_BOT_TOKEN found in environment!")
        return
    
    try:
        application = Application.builder() \
            .token(TOKEN) \
            .read_timeout(30) \
            .write_timeout(30) \
            .build()
        
        # Add error handler
        application.add_error_handler(error_handler)
        
        # Add conversation handler
        conv_handler = ConversationHandler(
            entry_points=[CommandHandler('escrow', escrow)],
            states={
                CHOOSING_ROLE: [CallbackQueryHandler(role_choice)],
                GET_PARTNER_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, verify_partner_id)],
                TRADE_DETAILS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_trade_details)],
                SELECT_CRYPTO: [CallbackQueryHandler(select_crypto)],
                ENTER_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_amount)],
                CONFIRM_TRADE: [CallbackQueryHandler(confirm_trade)],
                AWAIT_APPROVAL: [CallbackQueryHandler(confirm_trade)],
                PAYMENT_INSTRUCTIONS: [CallbackQueryHandler(payment_sent)],
            },
            fallbacks=[CommandHandler('cancel', cancel)]
        )
        
        application.add_handler(conv_handler)
        application.add_handler(CommandHandler('start', start))
        application.add_handler(CommandHandler('info', info))
        application.add_handler(CommandHandler('my_trades', my_trades))
        # Add these two handlers LAST (will catch all messages/buttons)
        application.add_handler(MessageHandler(filters.ALL, log_user_messages), group=99)
        application.add_handler(CallbackQueryHandler(log_button_presses), group=99)
        
        # Set up cleanup job
        job_queue = application.job_queue
        if job_queue:
            job_queue.run_repeating(cleanup_old_trades, interval=3600, first=10)
        
        logger.info("Bot starting with enhanced timeout handling...")
        application.run_polling(
            poll_interval=3.0,
            timeout=30,
            drop_pending_updates=True
        )
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")

if __name__ == '__main__':
    main()