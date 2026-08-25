"""Daily verse and quote content for the Sanctuary page.

The daily verse comes live from BibleGateway's official Verse-of-the-Day
feed (NIV).  The verses below are a fallback for offline days only, drawn
from the World English Bible, which is public domain — the fallback is
labelled WEB so the page never claims NIV text it did not fetch.

Quotes are short attributed sayings; the pick rotates deterministically by
IST date so the same day always shows the same pair.
"""

VOTD_URL = "https://www.biblegateway.com/votd/get/?format=json&version=NIV"

# (reference, text) — World English Bible, public domain.
FALLBACK_VERSES = [
    (
        "Psalm 23:1-2",
        "The LORD is my shepherd; I shall lack nothing. He makes me lie down in green pastures. He leads me beside still waters.",
    ),
    (
        "Isaiah 41:10",
        "Don't you be afraid, for I am with you. Don't be dismayed, for I am your God. I will strengthen you. Yes, I will help you. Yes, I will uphold you with the right hand of my righteousness.",
    ),
    (
        "Philippians 4:6-7",
        "In nothing be anxious, but in everything, by prayer and petition with thanksgiving, let your requests be made known to God. And the peace of God, which surpasses all understanding, will guard your hearts and your thoughts in Christ Jesus.",
    ),
    (
        "Jeremiah 29:11",
        "For I know the thoughts that I think toward you, says the LORD, thoughts of peace, and not of evil, to give you hope and a future.",
    ),
    ("Psalm 46:1", "God is our refuge and strength, a very present help in trouble."),
    (
        "Proverbs 3:5-6",
        "Trust in the LORD with all your heart, and don't lean on your own understanding. In all your ways acknowledge him, and he will make your paths straight.",
    ),
    ("Matthew 11:28", "Come to me, all you who labor and are heavily burdened, and I will give you rest."),
    (
        "Joshua 1:9",
        "Haven't I commanded you? Be strong and courageous. Don't be afraid. Don't be dismayed, for the LORD your God is with you wherever you go.",
    ),
    ("Psalm 118:24", "This is the day that the LORD has made. We will rejoice and be glad in it!"),
    (
        "Romans 8:28",
        "We know that all things work together for good for those who love God, for those who are called according to his purpose.",
    ),
    (
        "Lamentations 3:22-23",
        "It is because of the LORD's loving kindnesses that we are not consumed, because his compassion doesn't fail. They are new every morning. Great is your faithfulness.",
    ),
    (
        "Psalm 121:1-2",
        "I will lift up my eyes to the hills. Where does my help come from? My help comes from the LORD, who made heaven and earth.",
    ),
    ("2 Corinthians 12:9", "My grace is sufficient for you, for my power is made perfect in weakness."),
    ("Psalm 37:4", "Also delight yourself in the LORD, and he will give you the desires of your heart."),
    (
        "Isaiah 40:31",
        "But those who wait for the LORD will renew their strength. They will mount up with wings like eagles. They will run, and not be weary. They will walk, and not faint.",
    ),
    (
        "Zephaniah 3:17",
        "The LORD, your God, is among you, a mighty one who will save. He will rejoice over you with joy. He will calm you in his love. He will rejoice over you with singing.",
    ),
    ("Psalm 34:8", "Oh taste and see that the LORD is good. Blessed is the man who takes refuge in him."),
    (
        "John 14:27",
        "Peace I leave with you. My peace I give to you; not as the world gives, I give to you. Don't let your heart be troubled, neither let it be fearful.",
    ),
    (
        "Psalm 91:1-2",
        'He who dwells in the secret place of the Most High will rest in the shadow of the Almighty. I will say of the LORD, "He is my refuge and my fortress; my God, in whom I trust."',
    ),
    ("Philippians 4:13", "I can do all things through Christ who strengthens me."),
    ("Psalm 16:8", "I have set the LORD always before me. Because he is at my right hand, I shall not be moved."),
    ("1 Peter 5:7", "Casting all your worries on him, because he cares for you."),
    (
        "Psalm 27:1",
        "The LORD is my light and my salvation. Whom shall I fear? The LORD is the strength of my life. Of whom shall I be afraid?",
    ),
    ("Galatians 6:9", "Let's not be weary in doing good, for we will reap in due season if we don't give up."),
    (
        "Psalm 55:22",
        "Cast your burden on the LORD and he will sustain you. He will never allow the righteous to be moved.",
    ),
    (
        "Micah 6:8",
        "He has shown you, O man, what is good. What does the LORD require of you, but to act justly, to love mercy, and to walk humbly with your God?",
    ),
    ("Psalm 4:8", "In peace I will both lay myself down and sleep, for you alone, LORD, make me live in safety."),
    (
        "Matthew 6:33",
        "But seek first God's Kingdom and his righteousness, and all these things will be given to you as well.",
    ),
    (
        "Psalm 100:4-5",
        "Enter into his gates with thanksgiving, and into his courts with praise. Give thanks to him, and bless his name. For the LORD is good. His loving kindness endures forever, his faithfulness to all generations.",
    ),
    (
        "Deuteronomy 31:8",
        "The LORD himself is who goes before you. He will be with you. He will not fail you nor forsake you. Don't be afraid. Don't be discouraged.",
    ),
]

# (text, author) — short attributed sayings for the morning card.
QUOTES = [
    ("The best time to plant a tree was twenty years ago. The second best time is now.", "Chinese proverb"),
    ("Adopt the pace of nature: her secret is patience.", "Ralph Waldo Emerson"),
    ("It always seems impossible until it's done.", "Nelson Mandela"),
    ("What you do today can improve all your tomorrows.", "Ralph Marston"),
    ("Gratitude turns what we have into enough.", "Anonymous"),
    ("A river cuts through rock, not because of its power, but because of its persistence.", "James N. Watkins"),
    ("Peace comes from within. Do not seek it without.", "Buddha"),
    ("The little things? The little moments? They aren't little.", "Jon Kabat-Zinn"),
    ("Do the best you can until you know better. Then when you know better, do better.", "Maya Angelou"),
    ("In every walk with nature one receives far more than he seeks.", "John Muir"),
    ("Happiness is not something ready made. It comes from your own actions.", "Dalai Lama"),
    ("Well done is better than well said.", "Benjamin Franklin"),
    ("The days are long, but the years are short.", "Gretchen Rubin"),
    ("Nature does not hurry, yet everything is accomplished.", "Lao Tzu"),
    ("Start where you are. Use what you have. Do what you can.", "Arthur Ashe"),
    ("A family is a little world created by love.", "Anonymous"),
    (
        'Courage doesn\'t always roar. Sometimes courage is the quiet voice at the end of the day saying, "I will try again tomorrow."',
        "Mary Anne Radmacher",
    ),
    ("We make a living by what we get, but we make a life by what we give.", "Winston Churchill"),
    ("Enjoy the little things, for one day you may look back and realize they were the big things.", "Robert Brault"),
    ("He who plants a garden plants happiness.", "Chinese proverb"),
    ("The secret of getting ahead is getting started.", "Mark Twain"),
    ("Kind words are short and easy to speak, but their echoes are truly endless.", "Mother Teresa"),
    ("Every day may not be good, but there is something good in every day.", "Alice Morse Earle"),
    ("Turn your face to the sun and the shadows fall behind you.", "Maori proverb"),
    ("Little by little, one travels far.", "Spanish proverb"),
    ("The most wasted of all days is one without laughter.", "E. E. Cummings"),
    ("To plant a garden is to believe in tomorrow.", "Audrey Hepburn"),
    ("What we think, we become.", "Buddha"),
    ("Act as if what you do makes a difference. It does.", "William James"),
    ("A good laugh is sunshine in the house.", "William Makepeace Thackeray"),
    ("Rest and be thankful.", "William Wordsworth"),
    ("Life is really simple, but we insist on making it complicated.", "Confucius"),
    ("An early-morning walk is a blessing for the whole day.", "Henry David Thoreau"),
    ("Where there is love there is life.", "Mahatma Gandhi"),
    ("Perseverance is not a long race; it is many short races one after the other.", "Walter Elliot"),
    ("The quieter you become, the more you can hear.", "Ram Dass"),
    ("Wherever you go, go with all your heart.", "Confucius"),
    ("Children are the anchors that hold a mother to life.", "Sophocles"),
    ("A contented mind is the greatest blessing a man can enjoy in this world.", "Joseph Addison"),
    ("Look deep into nature, and then you will understand everything better.", "Albert Einstein"),
    ("It is not how much we have, but how much we enjoy, that makes happiness.", "Charles Spurgeon"),
    ("Hope is the thing with feathers that perches in the soul.", "Emily Dickinson"),
    ("Do small things with great love.", "Mother Teresa"),
    ("The sun does not shine for a few trees and flowers, but for the wide world's joy.", "Henry Ward Beecher"),
    ("You are never too old to set another goal or to dream a new dream.", "C. S. Lewis"),
    ("Music washes away from the soul the dust of everyday life.", "Berthold Auerbach"),
    ("Slow down and everything you are chasing will come around and catch you.", "John De Paola"),
    ("Each morning we are born again. What we do today is what matters most.", "Buddhist saying"),
    ("The family is one of nature's masterpieces.", "George Santayana"),
    ("Success is the sum of small efforts, repeated day in and day out.", "Robert Collier"),
    ("Keep your face always toward the sunshine, and shadows will fall behind you.", "Walt Whitman"),
    ("Simplicity is the ultimate sophistication.", "Leonardo da Vinci"),
    ("A tree is known by its fruit; a man by his deeds.", "Saint Basil"),
    ("Write it on your heart that every day is the best day in the year.", "Ralph Waldo Emerson"),
    ("The earth has music for those who listen.", "George Santayana"),
    ("Patience is bitter, but its fruit is sweet.", "Jean-Jacques Rousseau"),
    ("However difficult life may seem, there is always something you can do and succeed at.", "Stephen Hawking"),
    ("When you arise in the morning, think of what a precious privilege it is to be alive.", "Marcus Aurelius"),
    ("Home is the nicest word there is.", "Laura Ingalls Wilder"),
    (
        "Let us be grateful to the people who make us happy; they are the charming gardeners who make our souls blossom.",
        "Marcel Proust",
    ),
]
