import logging
import json
import os
import time
import asyncio
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, filters,
    CallbackQueryHandler, ContextTypes, ConversationHandler
)

# Импорт конфигурации
from config import (
    TELEGRAM_TOKEN, TMDB_API_KEY, TMDB_SEARCH_URL, 
    TMDB_MOVIE_URL, TMDB_GENRE_URL, TMDB_DISCOVER_URL, DATA_FILE
)

# --- Глобальный HTTP клиент ---
http_client = None

# --- Логирование ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Состояния для ConversationHandler ---
ADD_TITLE, ADD_CHOICE = range(2)
RECOMMEND_CHOOSE, RECOMMEND_FILM_PICK, RECOMMEND_GENRE_PICK = range(10, 13)
RECOMMEND_MORE_FILM, RECOMMEND_MORE_GENRE = range(15, 17)
LIST_SHOW, LIST_DELETE = range(20, 22)

# --- Глобальная клавиатура ---
global_keyboard = ReplyKeyboardMarkup([
    ['/add', '/list', '/recommend'],
    ['/help']
], resize_keyboard=True)

# --- Rate Limiting ---
class RateLimiter:
    def __init__(self, max_requests=10, time_window=1.0):
        self.max_requests = max_requests
        self.time_window = time_window
        self.requests = []
    
    async def acquire(self):
        now = time.time()
        # Удаляем старые запросы
        self.requests = [req_time for req_time in self.requests if now - req_time < self.time_window]
        
        if len(self.requests) >= self.max_requests:
            # Ждем до освобождения слота
            wait_time = self.time_window - (now - self.requests[0])
            if wait_time > 0:
                await asyncio.sleep(wait_time)
        
        self.requests.append(time.time())

# Глобальный rate limiter для TMDB API
tmdb_rate_limiter = RateLimiter(max_requests=8, time_window=1.0)  # 8 запросов в секунду

# --- Вспомогательные функции ---
def load_films():
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_films(data):
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# --- Получение жанров TMDB (кэшируем для ускорения) ---
_genre_cache = None
async def get_genres():
    global _genre_cache, http_client
    if _genre_cache:
        return _genre_cache
    if not http_client:
        http_client = httpx.AsyncClient(timeout=10.0, limits=httpx.Limits(max_keepalive_connections=5, max_connections=10))
    
    try:
        await tmdb_rate_limiter.acquire()
        resp = await http_client.get(TMDB_GENRE_URL, params={'api_key': TMDB_API_KEY, 'language': 'ru-RU'})
        if resp.status_code == 200:
            genres = {g['id']: g['name'] for g in resp.json().get('genres', [])}
            _genre_cache = genres
            return genres
    except Exception as e:
        logger.error(f"Error getting genres: {e}")
    return {}

# --- Команды ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        'Привет! Я бот для учёта просмотренных фильмов.\n'
        'Доступные команды:\n'
        '/add - добавить фильм\n'
        '/list - список фильмов\n'
        '/recommend - рекомендации\n'
        '/help - помощь',
        reply_markup=global_keyboard
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        'По всем вопросам пишите @KiyoponAI\n\n'
        'Доступные команды:\n'
        '/add - добавить фильм\n'
        '/list - список фильмов\n'
        '/recommend - рекомендации\n'
        '/help - помощь',
        reply_markup=global_keyboard
    )

# --- Добавление фильма ---
async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('Введите название фильма:', reply_markup=global_keyboard)
    return ADD_TITLE

async def add_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_time = time.time()
    title = update.message.text.strip()
    context.user_data['add_title'] = title
    logger.info(f"Searching for film: {title}")
    try:
        global http_client
        if not http_client:
            http_client = httpx.AsyncClient(timeout=10.0, limits=httpx.Limits(max_keepalive_connections=5, max_connections=10))
        
        await tmdb_rate_limiter.acquire()
        resp = await http_client.get(TMDB_SEARCH_URL, params={'api_key': TMDB_API_KEY, 'query': title, 'language': 'ru-RU'})
        resp.raise_for_status()
        results = resp.json().get('results', [])
        logger.info(f"Found {len(results)} results for '{title}' in {time.time() - start_time:.2f}s")
        if not results:
            await update.message.reply_text('Фильм не найден. Попробуйте ещё раз или /cancel.', reply_markup=global_keyboard)
            return ADD_TITLE
        if len(results) == 1:
            logger.info(f"Single result found, adding: {results[0]['title']}")
            return await add_save_film(update, context, results[0])
        keyboard = []
        for i, film in enumerate(results[:5]):
            year = film.get('release_date', '')[:4]
            keyboard.append([InlineKeyboardButton(f"{film['title']} ({year})", callback_data=str(i))])
        reply_markup = InlineKeyboardMarkup(keyboard)
        context.user_data['add_results'] = results
        await update.message.reply_text('Выберите нужный фильм:', reply_markup=reply_markup)
        return ADD_CHOICE
    except httpx.RequestException as e:
        logger.error(f"TMDB API error: {e}")
        await update.message.reply_text('Ошибка при поиске фильма. Попробуйте ещё раз или /cancel.', reply_markup=global_keyboard)
        return ADD_TITLE
    except Exception as e:
        logger.error(f"Unexpected error in add_title: {e}")
        await update.message.reply_text('Произошла ошибка. Попробуйте ещё раз или /cancel.', reply_markup=global_keyboard)
        return ADD_TITLE

async def add_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    # Проверяем, есть ли данные для добавления фильма
    if 'add_results' not in context.user_data:
        await query.message.edit_text('Ошибка: данные о фильмах не найдены. Попробуйте снова.')
        return ConversationHandler.END
    
    try:
        idx = int(query.data)
        film = context.user_data['add_results'][idx]
        return await add_save_film(query, context, film)
    except (ValueError, IndexError) as e:
        logger.error(f"Error in add_choice: {e}")
        await query.message.edit_text('Ошибка при выборе фильма. Попробуйте снова.')
        return ConversationHandler.END

async def add_save_film(update_or_query, context, film):
    try:
        user_id = str(update_or_query.from_user.id)
        films = load_films()
        user_films = films.get(user_id, [])
        
        # Проверка на дубли
        if any(f['id'] == film['id'] for f in user_films):
            await update_or_query.message.reply_text('Этот фильм уже есть в вашем списке.', reply_markup=global_keyboard)
            return ConversationHandler.END
            
        # Получаем жанры
        genres = await get_genres()
        genre_names = [genres.get(gid, '') for gid in film.get('genre_ids', [])]
        genre_str = f" ({', '.join(genre_names)})" if genre_names else ''
        
        # Сохраняем
        user_films.append({'id': film['id'], 'title': film['title'], 'genres': genre_names})
        films[user_id] = user_films
        save_films(films)
        
        logger.info(f"Film added for user {user_id}: {film['title']}")
        await update_or_query.message.reply_text(f'Фильм "{film["title"]}"{genre_str} добавлен!', reply_markup=global_keyboard)
        return ConversationHandler.END
        
    except Exception as e:
        logger.error(f"Error saving film: {e}")
        await update_or_query.message.reply_text('Ошибка при сохранении фильма. Попробуйте ещё раз.', reply_markup=global_keyboard)
        return ConversationHandler.END

async def add_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('Добавление отменено.', reply_markup=global_keyboard)
    return ConversationHandler.END

# --- Рекомендации ---
async def recommend_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton('По фильму', callback_data='by_film')],
        [InlineKeyboardButton('По жанру', callback_data='by_genre')],
    ]
    await update.message.reply_text('Какой тип рекомендации вас интересует?', reply_markup=InlineKeyboardMarkup(keyboard))
    return RECOMMEND_CHOOSE

async def recommend_choose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == 'by_film':
        # Список фильмов пользователя
        user_id = str(query.from_user.id)
        films = load_films().get(user_id, [])
        if not films:
            await query.message.edit_text('У вас нет фильмов для рекомендаций.')
            return ConversationHandler.END
        keyboard = [[InlineKeyboardButton(f["title"], callback_data=str(i))] for i, f in enumerate(films)]
        await query.message.edit_text('Выберите фильм:', reply_markup=InlineKeyboardMarkup(keyboard))
        context.user_data['recommend_films'] = films
        return RECOMMEND_FILM_PICK
    elif query.data == 'by_genre':
        genres = await get_genres()
        logger.info(f"Available genres: {genres}")
        keyboard = [[InlineKeyboardButton(name, callback_data=str(gid))] for gid, name in genres.items()]
        await query.message.edit_text('Выберите жанр:', reply_markup=InlineKeyboardMarkup(keyboard))
        return RECOMMEND_GENRE_PICK
    else:
        await query.message.edit_text('Неизвестный выбор.')
        return ConversationHandler.END

async def recommend_film_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_time = time.time()
    query = update.callback_query
    await query.answer()
    if query.data == "close_recommendations":
        await query.message.edit_text('Рекомендации закрыты.')
        return ConversationHandler.END
    elif query.data.startswith("add_rec_"):
        # Обработка нажатия на фильм в рекомендациях - открываем ссылку на TMDB
        film_id = query.data.split("_")[2]
        tmdb_url = f"https://www.themoviedb.org/movie/{film_id}"
        await query.message.edit_text(f'Открываю фильм на TMDB:\n{tmdb_url}')
        return ConversationHandler.END
    elif query.data.startswith("film_page_"):
        page = int(query.data.split("_")[2])
        context.user_data['film_page'] = page
        film = context.user_data['recommend_film']
    elif query.data == "more_film":
        page = context.user_data.get('film_page', 1)
        page += 1
        context.user_data['film_page'] = page
        film = context.user_data['recommend_film']
    else:
        try:
            idx = int(query.data)
            film = context.user_data['recommend_films'][idx]
            context.user_data['recommend_film'] = film
            context.user_data['film_page'] = 1
        except (ValueError, IndexError):
            await query.message.edit_text('Неизвестный выбор.')
            return ConversationHandler.END
    try:
        items_per_page = 5
        bot_page = context.user_data['film_page']
        user_id = str(query.from_user.id)
        user_films = set(f['id'] for f in load_films().get(user_id, []))
        
        # Упрощенный поиск - берем первую страницу и фильтруем
        tmdb_page = (bot_page - 1) // 4 + 1
        tmdb_results_offset = ((bot_page - 1) % 4) * items_per_page
        
        global http_client
        if not http_client:
            http_client = httpx.AsyncClient(timeout=10.0, limits=httpx.Limits(max_keepalive_connections=5, max_connections=10))
        
        await tmdb_rate_limiter.acquire()
        resp = await http_client.get(f'{TMDB_MOVIE_URL}{film["id"]}/recommendations', 
                                params={'api_key': TMDB_API_KEY, 'language': 'ru-RU', 'page': tmdb_page})
        resp.raise_for_status()
        results = resp.json().get('results', [])
        
        # Фильтруем фильмы, которые пользователь уже видел
        new_films = [f for f in results if f['id'] not in user_films]
        
        # Берем нужную порцию для текущей страницы
        start_idx = tmdb_results_offset
        end_idx = start_idx + items_per_page
        page_films = new_films[start_idx:end_idx]
        
        if not page_films:
            # Если на текущей странице нет новых фильмов, берем любые
            page_films = results[start_idx:end_idx]
        
        if not page_films:
            await query.message.edit_text('Больше рекомендаций нет.', reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Закрыть", callback_data="close_recommendations")
            ]]))
            return RECOMMEND_MORE_FILM
        
        text = f'Рекомендации по фильму "{film["title"]}":\n\n'
        keyboard = []
        
        genres_dict = await get_genres()
        for i, rec_film in enumerate(page_films):
            genres = rec_film.get('genre_ids', [])
            genre_names = [genres_dict.get(gid, '') for gid in genres if genres_dict.get(gid)]
            genre_text = f" ({', '.join(genre_names)})" if genre_names else ""
            text += f"{i+1}. {rec_film['title']}{genre_text}\n"
            tmdb_url = f"https://www.themoviedb.org/movie/{rec_film['id']}"
            keyboard.append([InlineKeyboardButton(f"{i+1}. {rec_film['title']}", url=tmdb_url)])
        
        # Добавляем навигацию
        nav_buttons = []
        if bot_page > 1:
            nav_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"film_page_{bot_page-1}"))
        nav_buttons.append(InlineKeyboardButton("➡️ Ещё", callback_data="more_film"))
        nav_buttons.append(InlineKeyboardButton("❌ Закрыть", callback_data="close_recommendations"))
        keyboard.append(nav_buttons)
        
        logger.info(f"Film recommendations generated in {time.time() - start_time:.2f}s")
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        return RECOMMEND_MORE_FILM
        
    except Exception as e:
        logger.error(f"Error getting film recommendations: {e}")
        await query.message.edit_text('Ошибка при получении рекомендаций.', reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Закрыть", callback_data="close_recommendations")
        ]]))
        return RECOMMEND_MORE_FILM

async def recommend_genre_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_time = time.time()
    query = update.callback_query
    await query.answer()
    
    logger.info(f"Genre pick callback data: {query.data}")
    
    if query.data == "close_recommendations":
        await query.message.edit_text('Рекомендации закрыты.')
        return ConversationHandler.END
    elif query.data.startswith("add_rec_"):
        # Обработка нажатия на фильм в рекомендациях - открываем ссылку на TMDB
        film_id = query.data.split("_")[2]
        tmdb_url = f"https://www.themoviedb.org/movie/{film_id}"
        await query.message.edit_text(f'Открываю фильм на TMDB:\n{tmdb_url}')
        return ConversationHandler.END
    elif query.data == "more_genre":
        page = context.user_data.get('genre_page', 1)
        page += 1
        context.user_data['genre_page'] = page
        genre_id = context.user_data['recommend_genre_id']
    elif query.data.startswith("genre_page_"):
        page = int(query.data.split("_")[2])
        context.user_data['genre_page'] = page
        genre_id = context.user_data['recommend_genre_id']
    else:
        try:
            genre_id = int(query.data)
            context.user_data['recommend_genre_id'] = genre_id
            context.user_data['genre_page'] = 1
            logger.info(f"Selected genre ID: {genre_id}")
        except ValueError:
            logger.error(f"Invalid genre selection: {query.data}")
            await query.message.edit_text('Неизвестный выбор.')
            return ConversationHandler.END
    
    try:
        genres_dict = await get_genres()
        genre_name = genres_dict.get(genre_id, 'Неизвестный жанр')
        page = context.user_data['genre_page']
        user_id = str(query.from_user.id)
        user_films = set(f['id'] for f in load_films().get(user_id, []))
        
        global http_client
        if not http_client:
            http_client = httpx.AsyncClient(timeout=10.0, limits=httpx.Limits(max_keepalive_connections=5, max_connections=10))
        
        await tmdb_rate_limiter.acquire()
        resp = await http_client.get(f'{TMDB_DISCOVER_URL}', 
                                params={'api_key': TMDB_API_KEY, 'language': 'ru-RU', 'with_genres': genre_id, 'page': page})
        resp.raise_for_status()
        results = resp.json().get('results', [])
        
        # Фильтруем фильмы, которые пользователь уже видел
        new_films = [f for f in results if f['id'] not in user_films]
        
        if not new_films:
            # Если нет новых фильмов, берем любые
            new_films = results[:5]
        
        if not new_films:
            await query.message.edit_text('Больше рекомендаций нет.', reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Закрыть", callback_data="close_recommendations")
            ]]))
            return RECOMMEND_MORE_GENRE
        
        text = f'Рекомендации по жанру "{genre_name}":\n\n'
        keyboard = []
        
        for i, rec_film in enumerate(new_films):
            genres = rec_film.get('genre_ids', [])
            genre_names = [genres_dict.get(gid, '') for gid in genres if genres_dict.get(gid)]
            genre_text = f" ({', '.join(genre_names)})" if genre_names else ""
            text += f"{i+1}. {rec_film['title']}{genre_text}\n"
            tmdb_url = f"https://www.themoviedb.org/movie/{rec_film['id']}"
            keyboard.append([InlineKeyboardButton(f"{i+1}. {rec_film['title']}", url=tmdb_url)])
        
        # Добавляем навигацию
        nav_buttons = []
        if page > 1:
            nav_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"genre_page_{page-1}"))
        nav_buttons.append(InlineKeyboardButton("➡️ Ещё", callback_data="more_genre"))
        nav_buttons.append(InlineKeyboardButton("❌ Закрыть", callback_data="close_recommendations"))
        keyboard.append(nav_buttons)
        
        logger.info(f"Genre recommendations generated in {time.time() - start_time:.2f}s")
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        return RECOMMEND_MORE_GENRE
        
    except Exception as e:
        logger.error(f"Error getting genre recommendations: {e}")
        await query.message.edit_text('Ошибка при получении рекомендаций.', reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Закрыть", callback_data="close_recommendations")
        ]]))
        return RECOMMEND_MORE_GENRE

async def recommend_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('Рекомендации отменены.', reply_markup=global_keyboard)
    return ConversationHandler.END

async def close_recommendations(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.edit_text('Рекомендации закрыты.')
    return ConversationHandler.END

async def list_films(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    films = load_films().get(user_id, [])
    if not films:
        await update.message.reply_text('Ваш список фильмов пуст.', reply_markup=global_keyboard)
        return ConversationHandler.END
    
    # Сохраняем фильмы в контексте для последующего удаления
    context.user_data['list_films'] = films
    
    text = 'Ваши фильмы:\n'
    for i, film in enumerate(films, 1):
        genres = f" ({', '.join(film['genres'])})" if film['genres'] else ''
        text += f"{i}. {film['title']}{genres}\n"
    
    # Добавляем кнопку "Удалить" внизу списка
    keyboard = [[InlineKeyboardButton("🗑️ Удалить фильм", callback_data="show_delete_interface")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(text, reply_markup=reply_markup)
    return LIST_SHOW

# --- ConversationHandler для /add ---
add_conv = ConversationHandler(
    entry_points=[CommandHandler('add', add_start)],
    states={
        ADD_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_title)],
        ADD_CHOICE: [CallbackQueryHandler(add_choice)],
    },
    fallbacks=[
        CommandHandler('cancel', add_cancel),
        CommandHandler('start', start),
        CommandHandler('recommend', recommend_start),
        CommandHandler('list', list_films),
    ],
    per_message=False,
)

# --- Удаление фильмов ---
async def list_delete_film(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "show_delete_interface":
        # Показываем интерфейс удаления
        films = context.user_data.get('list_films', [])
        if not films:
            await query.message.reply_text('Ваш список фильмов пуст.', reply_markup=global_keyboard)
            return ConversationHandler.END
        
        text = 'Выберите фильм для удаления:\n'
        keyboard = []
        for i, film in enumerate(films):
            # Ограничиваем длину текста кнопки
            button_text = film['title'][:30] + "..." if len(film['title']) > 30 else film['title']
            keyboard.append([InlineKeyboardButton(f"❌ {button_text}", callback_data=f"delete_{i}")])
        
        # Добавляем кнопку "Отмена"
        keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_delete")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.edit_text(text, reply_markup=reply_markup)
        return LIST_SHOW
    
    elif query.data == "cancel_delete":
        # Возвращаемся к обычному списку
        films = context.user_data.get('list_films', [])
        text = 'Ваши фильмы:\n'
        for i, film in enumerate(films, 1):
            genres = f" ({', '.join(film['genres'])})" if film['genres'] else ''
            text += f"{i}. {film['title']}{genres}\n"
        
        keyboard = [[InlineKeyboardButton("🗑️ Удалить фильм", callback_data="show_delete_interface")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.edit_text(text, reply_markup=reply_markup)
        return LIST_SHOW
    
    elif query.data == "show_updated_list":
        # Показываем обновленный список
        films = context.user_data.get('list_films', [])
        
        if not films:
            await query.message.reply_text('Ваш список фильмов пуст.', reply_markup=global_keyboard)
            return ConversationHandler.END
        
        text = 'Обновленный список фильмов:\n'
        for i, film in enumerate(films, 1):
            genres = f" ({', '.join(film['genres'])})" if film['genres'] else ''
            text += f"{i}. {film['title']}{genres}\n"
        
        keyboard = [[InlineKeyboardButton("🗑️ Удалить фильм", callback_data="show_delete_interface")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.edit_text(text, reply_markup=reply_markup)
        return LIST_SHOW
    
    elif query.data.startswith("delete_"):
        try:
            # Получаем индекс фильма для удаления
            film_index = int(query.data.split("_")[1])
            films = context.user_data.get('list_films', [])
            
            if film_index >= len(films):
                await query.message.reply_text("Ошибка: фильм не найден.", reply_markup=global_keyboard)
                return ConversationHandler.END
            
            # Получаем информацию о фильме перед удалением
            film_to_delete = films[film_index]
            user_id = str(query.from_user.id)
            
            # Удаляем фильм из списка
            films.pop(film_index)
            
            # Сохраняем обновленный список
            all_films = load_films()
            all_films[user_id] = films
            save_films(all_films)
            
            logger.info(f"Film deleted for user {user_id}: {film_to_delete['title']}")
            
            # Отправляем подтверждение удаления
            await query.message.reply_text(f"Фильм '{film_to_delete['title']}' удален из вашего списка!", reply_markup=global_keyboard)
            
            # Если остались фильмы, показываем обновленный список
            if films:
                keyboard = [[InlineKeyboardButton("📋 Показать обновленный список", callback_data="show_updated_list")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.message.reply_text("Хотите посмотреть обновленный список?", reply_markup=reply_markup)
                context.user_data['list_films'] = films  # Обновляем список в контексте
                return LIST_SHOW
            else:
                await query.message.reply_text("Ваш список фильмов теперь пуст.", reply_markup=global_keyboard)
                return ConversationHandler.END
            
        except (ValueError, IndexError) as e:
            logger.error(f"Error deleting film: {e}")
            await query.message.reply_text("Ошибка при удалении фильма.", reply_markup=global_keyboard)
            return ConversationHandler.END

# --- ConversationHandler для /list ---
list_conv = ConversationHandler(
    entry_points=[CommandHandler('list', list_films)],
    states={
        LIST_SHOW: [CallbackQueryHandler(list_delete_film)],
    },
    fallbacks=[
        CommandHandler('cancel', add_cancel),
        CommandHandler('start', start),
        CommandHandler('recommend', recommend_start),
        CommandHandler('list', list_films),
    ],
    per_message=False,
)

# --- ConversationHandler для /recommend ---
recommend_conv = ConversationHandler(
    entry_points=[CommandHandler('recommend', recommend_start)],
    states={
        RECOMMEND_CHOOSE: [CallbackQueryHandler(recommend_choose)],
        RECOMMEND_FILM_PICK: [CallbackQueryHandler(recommend_film_pick)],
        RECOMMEND_GENRE_PICK: [CallbackQueryHandler(recommend_genre_pick)],
        RECOMMEND_MORE_FILM: [CallbackQueryHandler(recommend_film_pick)],
        RECOMMEND_MORE_GENRE: [CallbackQueryHandler(recommend_genre_pick)],
    },
    fallbacks=[
        CommandHandler('cancel', recommend_cancel),
        CommandHandler('start', start),
        CommandHandler('recommend', recommend_start),
        CommandHandler('list', list_films),
    ],
    per_message=False,
)

async def universal_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data
    # Удаление фильмов
    if data.startswith('delete_') or data in ['show_delete_interface', 'cancel_delete', 'show_updated_list']:
        return await list_delete_film(update, context)
    # Рекомендации: выбор типа
    elif data in ['by_film', 'by_genre']:
        return await recommend_choose(update, context)
    # Рекомендации по фильму
    elif data.startswith('film_page_') or data == 'more_film' or data == 'close_recommendations' or data.startswith('add_rec_') or (data.isdigit() and 'recommend_films' in context.user_data):
        return await recommend_film_pick(update, context)
    # Рекомендации по жанру (первый выбор жанра или навигация)
    elif data.startswith('genre_page_') or data == 'more_genre' or data.startswith('add_rec_') or data == 'close_recommendations' or (data.isdigit() and 'recommend_genre_id' in context.user_data):
        return await recommend_genre_pick(update, context)
    # Добавление фильма (выбор из поиска)
    elif data.isdigit() and 'add_results' in context.user_data:
        return await add_choice(update, context)
    else:
        await update.callback_query.answer()
        await update.callback_query.message.edit_text('Сессия устарела, начните заново.')
        return ConversationHandler.END

# --- Основной запуск ---
async def cleanup():
    global http_client
    if http_client:
        await http_client.aclose()

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    # Сначала ConversationHandler для состояний
    app.add_handler(add_conv)
    app.add_handler(list_conv)
    app.add_handler(recommend_conv)
    
    # Потом команды
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('help', help_command))
    app.add_handler(CommandHandler('add', add_start))
    app.add_handler(CommandHandler('list', list_films))
    app.add_handler(CommandHandler('recommend', recommend_start))
    
    # Universal CallbackQueryHandler должен быть последним
    app.add_handler(CallbackQueryHandler(universal_callback_handler))
    
    # Обработка завершения работы
    app.add_handler(CommandHandler('shutdown', lambda u, c: cleanup()))
    
    # Запуск с обработкой ошибок
    try:
        app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Bot crashed: {e}")
    finally:
        # Очистка ресурсов
        import asyncio
        try:
            asyncio.run(cleanup())
        except:
            pass

if __name__ == '__main__':
    main() 