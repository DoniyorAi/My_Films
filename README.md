# MyFilmsbyKiyoponAi - Telegram Bot

Бот для учёта просмотренных фильмов с рекомендациями на основе TMDB API.

## Установка и настройка

1. **Клонируйте репозиторий**
   ```bash
   git clone <repository-url>
   cd MyFilmsbyKiyoponAi
   ```

2. **Создайте виртуальное окружение**
   ```bash
   python -m venv venv
   # Для Windows:
   venv\Scripts\activate
   # Для Linux/Mac:
   source venv/bin/activate
   ```

3. **Установите зависимости**
   ```bash
   pip install -r requirements.txt
   ```

4. **Настройте конфигурацию**
   ```bash
   # Скопируйте пример конфигурации
   cp config.example.py config.py
   
   # Отредактируйте config.py и добавьте свои токены:
   # - TELEGRAM_TOKEN - получите у @BotFather
   # - TMDB_API_KEY - получите на https://www.themoviedb.org/settings/api
   ```

5. **Запустите бота**
   ```bash
   python bot.py
   ```

## Функции бота

- `/add` - добавить фильм в список просмотренных
- `/list` - показать список фильмов с возможностью удаления
- `/recommend` - получить рекомендации по фильму или жанру
- `/help` - справка

## Безопасность

- Файл `config.py` с токенами добавлен в `.gitignore`
- Никогда не коммитьте реальные токены в репозиторий
- Используйте `config.example.py` как шаблон для настройки

## Структура проекта

```
MyFilmsbyKiyoponAi/
├── bot.py              # Основной код бота
├── config.py           # Конфигурация с токенами (не в репозитории)
├── config.example.py   # Пример конфигурации
├── requirements.txt    # Зависимости Python
├── films.json         # Данные пользователей (создается автоматически)
├── .gitignore         # Исключения для Git
└── README.md          # Этот файл
``` 