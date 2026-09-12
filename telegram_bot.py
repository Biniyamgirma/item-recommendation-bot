import asyncio
import sys
import logging
import os
from dotenv import load_dotenv

# Load environment variables from the .env file
load_dotenv()
import html
import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# NOTE: Please rotate/change your bot token and DB credentials after testing!
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")

if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
    raise RuntimeError("Set the TELEGRAM_BOT_TOKEN environment variable before starting the bot.")
if not DATABASE_URL or DATABASE_URL == "YOUR_DATABASE_URL_HERE":
    raise RuntimeError("Set the DATABASE_URL environment variable before starting the bot.")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)

TELEGRAM_MSG_LIMIT = 4000

# --------------------------------------------------------------------------
# Conversation states
# --------------------------------------------------------------------------

WAITING_ORDER_ID, WAITING_ITEM_PICK, WAITING_ORDER_ID_FOR_ITEM, WAITING_ITEM_ID, WAITING_MODE = range(5)

# --------------------------------------------------------------------------
# SQL
# --------------------------------------------------------------------------

ORDER_ITEMS_LIST_QUERY = text(
    """
    SELECT od.food_id AS food_id, f.name AS food_name
    FROM order_details od
    JOIN food f ON od.food_id = f.id
    WHERE od.order_id = :order_id
    """
)

ORDER_QUERY = text(
    """
    SELECT
        f.category_id,
        f.veg,
        f.price,
        TRIM(SUBSTRING_INDEX(r.name,'|',1)) AS restaurant_name,
        o.delivery_distance,
        o.delivery_address->>'$.longitude' AS longitude,
        o.delivery_address->>'$.latitude'  AS latitude
    FROM orders o
    JOIN order_details od ON o.id = od.order_id
    JOIN food f ON od.food_id = f.id
    JOIN restaurants r ON f.restaurant_id = r.id
    WHERE o.id = :order_id
    """
)

ORDER_ITEM_QUERY = text(
    """
    SELECT
        f.category_id,
        f.veg,
        f.price,
        TRIM(SUBSTRING_INDEX(r.name,'|',1)) AS restaurant_name,
        o.delivery_distance,
        o.delivery_address->>'$.longitude' AS longitude,
        o.delivery_address->>'$.latitude'  AS latitude
    FROM orders o
    JOIN order_details od ON o.id = od.order_id
    JOIN food f ON od.food_id = f.id
    JOIN restaurants r ON f.restaurant_id = r.id
    WHERE o.id = :order_id AND od.food_id = :food_id
    """
)

RECOMMENDATION_QUERY = text(
    """
    WITH recomend_item AS (
        SELECT
            f.id,
            f.name AS food_name,
            res.name AS restaurant_name,
            ca.id AS categories_id,
            ca.name AS syb_categories,
            res.longitude AS longitude,
            res.latitude AS latitude,
            f.price,
            f.discount,
            f.discount_type,
            f.veg,
            f.order_count,
            dz.name AS zone_name,
            COUNT(IF(r.rating = 1, r.id, NULL)) AS rating_1,
            COUNT(IF(r.rating = 2, r.id, NULL)) AS rating_2,
            COUNT(IF(r.rating = 3, r.id, NULL)) AS rating_3,
            COUNT(IF(r.rating = 4, r.id, NULL)) AS rating_4,
            COUNT(IF(r.rating = 5, r.id, NULL)) AS rating_5
        FROM food f
        JOIN restaurants res ON res.id = f.restaurant_id
        JOIN categories ca ON ca.id = f.category_id
        JOIN reviews r ON r.food_id = f.id
        LEFT JOIN categories sub_ca ON sub_ca.id = f.category_ids
        JOIN delivery_zones dz ON dz.id = res.z_id
        WHERE f.deleted_at IS NULL AND f.status = 1 AND res.status = 1 AND res.active = 1
        GROUP BY f.id
    ),
    filtered_item AS (
        SELECT
            ri.*,
            RANK() OVER (ORDER BY ri.order_count DESC) AS item_rank,
            ROUND((
                6371 * ACOS(
                    COS(RADIANS(ri.latitude)) * COS(RADIANS(:latitude))
                    * COS(RADIANS(:longitude) - RADIANS(ri.longitude))
                    + SIN(RADIANS(ri.latitude)) * SIN(RADIANS(:latitude))
                )
            ), 2) AS distance_from_customer
        FROM recomend_item ri
        WHERE ri.categories_id = :categories
          AND ri.price < (:price + 151)
          AND ri.price >= (:price - 150)
          AND ri.veg = :veg
          AND (ri.rating_5 <> 0 OR ri.rating_4 <> 0 OR ri.rating_3 <> 0)
          AND (
                6371 * ACOS(
                    COS(RADIANS(ri.latitude)) * COS(RADIANS(:latitude))
                    * COS(RADIANS(:longitude) - RADIANS(ri.longitude))
                    + SIN(RADIANS(ri.latitude)) * SIN(RADIANS(:latitude))
                )
              ) < GREATEST(:delivery_distance, 4)
    )
    SELECT
        fi.food_name,
        fi.restaurant_name,
        fi.syb_categories,
        fi.zone_name,
        fi.price,
        fi.discount,
        fi.discount_type,
        fi.veg,
        fi.order_count,
        fi.distance_from_customer,
        fi.rating_1, fi.rating_2, fi.rating_3, fi.rating_4, fi.rating_5,
        ROUND(
            (0.20 * (
                        (fi.rating_5 * 1.0) 
                        + (fi.rating_4 * 0.50) 
                        + (fi.rating_3 * 0.15)
                    ) / (fi.rating_5 + fi.rating_4 + fi.rating_3)) +
            (0.20 * (1.0 - (0.10 * (LEAST(fi.item_rank, 5) - 1)))) +
            (0.30 * ((GREATEST(:delivery_distance, 4) - (fi.distance_from_customer)) / GREATEST(:delivery_distance, 4))) +
            (0.10 * (1.0 - LEAST(ABS(fi.price - :price) / 150.0, 1.0)))
            +
              (0.20 * IF(fi.restaurant_name LIKE CONCAT('%', TRIM(SUBSTRING_INDEX(:restaurant_name, '|', 1)), '%'), 1, 0))
        , 2) * 100 AS recommendation_score
    FROM filtered_item fi
    ORDER BY recommendation_score DESC
    LIMIT 15;
    """
)

# --------------------------------------------------------------------------
# DB helpers
# --------------------------------------------------------------------------

async def start_over_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Reset state and present the main mode selection menu."""
    query = update.callback_query
    if query:
        await query.answer()
        context.user_data.clear()
        keyboard = [
            [InlineKeyboardButton("Recommend by Order ID", callback_data="mode_order")],
            [InlineKeyboardButton("Recommend by Order + Item ID", callback_data="mode_order_item")],
        ]
        await query.edit_message_text(
            "Okay, let's start over. How would you like to search?",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    return WAITING_MODE


async def run_df_async(query, params=None) -> pd.DataFrame:
    return await asyncio.to_thread(pd.read_sql_query, query, engine, params=params or {})


def build_info_obj(order_row: pd.Series) -> dict:
    return {
        "categories": int(order_row["category_id"]),
        "longitude": float(order_row["longitude"]),
        "latitude": float(order_row["latitude"]),
        "veg": int(order_row["veg"]),
        "restaurant_name": str(order_row["restaurant_name"]),
        "delivery_distance": int(order_row["delivery_distance"]),
        "price": float(order_row["price"]),
    }


def format_recommendations(df: pd.DataFrame) -> list[str]:
    """Turn every recommendation row into readable HTML text."""
    messages: list[str] = []
    current = ""
    for idx, row in df.iterrows():
        block = (
            f" <b>{html.escape(str(row['food_name']))}</b> — {html.escape(str(row['restaurant_name']))}\n"
            f"   Category: {html.escape(str(row['syb_categories']))} | Zone: {html.escape(str(row['zone_name']))}\n"
            f"   Price: {row['price']} | Veg: {'Yes' if row['veg'] else 'No'}\n"
            f"   Discount: {row['discount']} ({row['discount_type']})\n"
            f"   Distance: {row['distance_from_customer']} km | Orders: {row['order_count']}\n"
            f"   Ratings — 5:{row['rating_5']} 4:{row['rating_4']} 3:{row['rating_3']} "
            f"2:{row['rating_2']} 1:{row['rating_1']}\n"
            f"   ⭐ Score: {row['recommendation_score']}\n\n"
            f"------------------------------------------------------\n\n"
        )
        if len(current) + len(block) > TELEGRAM_MSG_LIMIT:
            messages.append(current)
            current = block
        else:
            current += block
    if current:
        messages.append(current)
    return messages


def start_over_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Start over", callback_data="start_over")]]
    )


async def send_recommendations(update: Update, order_row: pd.Series) -> None:
    chat = update.effective_chat
    try:
        info_obj = build_info_obj(order_row)
    except (TypeError, ValueError) as e:
        await chat.send_message(
            f"Could not read item details from the order: {e}",
            reply_markup=start_over_keyboard(),
        )
        return

    await chat.send_message("Crunching the numbers, one moment...", reply_markup=start_over_keyboard())

    try:
        rec_df = await run_df_async(RECOMMENDATION_QUERY, info_obj)
    except SQLAlchemyError as e:
        logger.exception("Recommendation query failed")
        await chat.send_message(
            f"Sorry, something went wrong while fetching recommendations: {e}",
            reply_markup=start_over_keyboard(),
        )
        return

    if rec_df.empty:
        await chat.send_message(
            "No matching recommendations were found for this item.",
            reply_markup=start_over_keyboard(),
        )
        return

    total_found = len(rec_df)
    top_df = rec_df.head(15)

    if total_found > 3:
        await chat.send_message(
            f"Found {total_found} matching recommendation(s); showing top {len(top_df)}.",
            reply_markup=start_over_keyboard(),
        )

    for msg in format_recommendations(top_df):
        await chat.send_message(msg, parse_mode="HTML", reply_markup=start_over_keyboard())

    await chat.send_message(f"Done — {len(top_df)} recommendation(s) shown.", reply_markup=start_over_keyboard())


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    keyboard = [
        [InlineKeyboardButton("Recommend by Order ID", callback_data="mode_order")],
        [InlineKeyboardButton("Recommend by Order + Item ID", callback_data="mode_order_item")],
    ]
    await update.message.reply_text(
        "Hi! I can recommend similar food items.\nHow would you like to search?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return WAITING_MODE


async def mode_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "mode_order":
        context.user_data["mode"] = "order"
        await query.edit_message_text("Please send me the <b>Order ID</b>.", parse_mode="HTML")
        return WAITING_ORDER_ID

    if query.data == "mode_order_item":
        context.user_data["mode"] = "order_item"
        await query.edit_message_text("Please send me the <b>Order ID</b>.", parse_mode="HTML")
        return WAITING_ORDER_ID_FOR_ITEM

    await query.edit_message_text("Unrecognized option. Send /start to try again.")
    return ConversationHandler.END


async def receive_order_id_only_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        order_id = int(update.message.text.strip())
    except (ValueError, AttributeError):
        await update.message.reply_text(
            "That doesn't look like a valid order id (numbers only). Try again:",
            reply_markup=start_over_keyboard(),
        )
        return WAITING_ORDER_ID

    await update.message.reply_text("Loading... ⏳", reply_markup=start_over_keyboard())

    try:
        items_df = await run_df_async(ORDER_ITEMS_LIST_QUERY, {"order_id": order_id})
    except SQLAlchemyError as e:
        logger.exception("Failed to look up order items")
        await update.message.reply_text(
            f"Database error while looking up the order: {e}",
            reply_markup=start_over_keyboard(),
        )
        return ConversationHandler.END

    if items_df.empty:
        await update.message.reply_text(
            "I couldn't find that order (or it has no items). Send /start to try again.",
            reply_markup=start_over_keyboard(),
        )
        return ConversationHandler.END

    if len(items_df) == 1:
        food_id = int(items_df.iloc[0]["food_id"])
        return await _lookup_item_and_recommend(update, order_id, food_id)

    context.user_data["order_id"] = order_id
    lines = ["This order has multiple items. Choose the item you want a recommendation for:\n"]
    for _, row in items_df.iterrows():
        lines.append(f"• ID {row['food_id']} — {row['food_name']}")
    lines.append("\nReply with the item id.")
    await update.message.reply_text("\n".join(lines), reply_markup=start_over_keyboard())
    return WAITING_ITEM_PICK


async def receive_item_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    order_id = context.user_data.get("order_id")
    try:
        food_id = int(update.message.text.strip())
    except (ValueError, AttributeError):
        await update.message.reply_text("Please reply with a valid numeric item id:", reply_markup=start_over_keyboard())
        return WAITING_ITEM_PICK

    await update.message.reply_text("Loading... ⏳", reply_markup=start_over_keyboard())
    return await _lookup_item_and_recommend(update, order_id, food_id)


async def receive_order_id_for_item_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        order_id = int(update.message.text.strip())
    except (ValueError, AttributeError):
        await update.message.reply_text("That doesn't look like a valid order id (numbers only). Try again:", reply_markup=start_over_keyboard())
        return WAITING_ORDER_ID_FOR_ITEM

    context.user_data["order_id"] = order_id
    await update.message.reply_text("Got it. Now send me the <b>Item ID</b>.", parse_mode="HTML", reply_markup=start_over_keyboard())
    return WAITING_ITEM_ID


async def receive_item_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    order_id = context.user_data.get("order_id")
    try:
        food_id = int(update.message.text.strip())
    except (ValueError, AttributeError):
        await update.message.reply_text("Please reply with a valid numeric item id:", reply_markup=start_over_keyboard())
        return WAITING_ITEM_ID

    await update.message.reply_text("Loading... ⏳", reply_markup=start_over_keyboard())
    return await _lookup_item_and_recommend(update, order_id, food_id)


async def _lookup_item_and_recommend(update: Update, order_id: int, food_id: int) -> int:
    try:
        order_df = await run_df_async(ORDER_ITEM_QUERY, {"order_id": order_id, "food_id": food_id})
    except SQLAlchemyError as e:
        logger.exception("Failed to look up order/item")
        await update.message.reply_text(f"⚠️ Database error while looking up that item: {e}")
        return ConversationHandler.END

    if order_df.empty:
        await update.message.reply_text(f"Item {food_id} isn't part of order {order_id}. Send /start to try again.", reply_markup=start_over_keyboard())
        return ConversationHandler.END

    await send_recommendations(update, order_df.iloc[0])
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Cancelled. Send /start to begin again.", reply_markup=start_over_keyboard())
    return ConversationHandler.END


async def fallback_error(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("Something unexpected happened. Send /start to try again.")


# --------------------------------------------------------------------------
# App wiring
# --------------------------------------------------------------------------

def main() -> None:
    # --- ASYNCIO SETUP FOR PYTHON 3.14 AND WINDOWS ---
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    # -------------------------------------------------

    app = Application.builder().token(BOT_TOKEN).build()

    # Define the handler here so we can reuse it easily!
    start_over_handler = CallbackQueryHandler(start_over_callback, pattern="^start_over$")

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            start_over_handler,
        ],
        states={
            WAITING_MODE: [
                CallbackQueryHandler(mode_chosen, pattern="^mode_"),
            ],
            WAITING_ORDER_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_order_id_only_mode),
            ],
            WAITING_ITEM_PICK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_item_pick),
            ],
            WAITING_ORDER_ID_FOR_ITEM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_order_id_for_item_mode),
            ],
            WAITING_ITEM_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_item_id),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            start_over_handler,
        ],
    )

    app.add_handler(conv)
    app.add_error_handler(fallback_error)

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()