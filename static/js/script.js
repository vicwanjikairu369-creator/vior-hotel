// Shared cart, order tracking, reviews, and invoice helpers.
if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => {
        navigator.serviceWorker.register('/service-worker.js').catch(error => {
            console.error('Vior Hotel offline support could not start:', error);
        });
    });
}

const SWAHILI_TEXT = {
    'Home': 'Mwanzo',
    'Menu': 'Menyu',
    'Experience': 'Huduma',
    'Reviews': 'Maoni',
    'Contact': 'Wasiliana',
    'Cart': 'Kikapu',
    'Sign in': 'Ingia',
    'Register': 'Jisajili',
    'My account': 'Akaunti yangu',
    'Back to menu': 'Rudi kwenye menyu',
    'Checkout': 'Lipa',
    'Time for Food · Kenyan fine dining, crafted daily': 'Wakati wa Chakula · Vyakula vya Kenya, vilivyoandaliwa kila siku',
    'Bold Kenyan comfort food, thoughtfully prepared and served in a refined, welcoming space.': 'Vyakula vitamu vya Kenya, vilivyoandaliwa kwa umakini na kutolewa kwa ukarimu.',
    'View Menu': 'Tazama Menyu',
    'Order Now': 'Agiza Sasa',
    "Chef's collection": 'Chaguo la Mpishi',
    'Comfort food, elevated.': 'Vyakula vitamu vya kipekee.',
    'Breakfast': 'Kiamsha kinywa',
    'Lunch & dinner': 'Chakula cha mchana na jioni',
    'Snacks & sweets': 'Vitafunwa na peremende',
    'New at Vior': 'Vipya Vior',
    'Add to Cart': 'Ongeza Kikapuni',
    'Search meals...': 'Tafuta chakula...',
    'Show saved meals': 'Onyesha vyakula vilivyohifadhiwa',
    'Your order': 'Agizo lako',
    'Order summary': 'Muhtasari wa agizo',
    'Checkout details': 'Maelezo ya malipo',
    'Delivery Method': 'Njia ya kupokea',
    'Pickup at Hotel': 'Chukua Hoteli',
    'Online Delivery': 'Uwasilishaji',
    'Special instructions': 'Maelekezo maalum',
    'Continue to secure payment': 'Endelea kulipa kwa usalama',
    'Track Order': 'Fuatilia agizo',
    'Track current order': 'Fuatilia agizo la sasa',
    'People keep coming back.': 'Wateja hurudi tena na tena.',
    'Stories, specials & offers.': 'Habari, vyakula maalum na ofa.',
    'From Vior Hotel': 'Kutoka Vior Hotel',
};
const originalTextNodes = new Map();
const visibleTextWalker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
        return node.parentElement?.closest('script,style,textarea') ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT;
    },
});
while (visibleTextWalker.nextNode()) {
    originalTextNodes.set(visibleTextWalker.currentNode, visibleTextWalker.currentNode.nodeValue);
}

function applyLanguage(language) {
    const swahili = language === 'sw';
    document.documentElement.lang = swahili ? 'sw' : 'en';
    for (const [node, original] of originalTextNodes) {
        const content = original.trim();
        const translated = swahili ? SWAHILI_TEXT[content] : null;
        if (translated) {
            const start = original.indexOf(content);
            node.nodeValue = `${original.slice(0, start)}${translated}${original.slice(start + content.length)}`;
        } else {
            node.nodeValue = original;
        }
    }
    document.querySelectorAll('[placeholder]').forEach(element => {
        if (!element.dataset.englishPlaceholder) {
            element.dataset.englishPlaceholder = element.placeholder;
        }
        element.placeholder = swahili
            ? SWAHILI_TEXT[element.dataset.englishPlaceholder] || element.dataset.englishPlaceholder
            : element.dataset.englishPlaceholder;
    });
    document.querySelectorAll('#siteLanguage').forEach(select => { select.value = swahili ? 'sw' : 'en'; });
}

let initialLanguage = document.documentElement.lang;
try {
    initialLanguage = localStorage.getItem('viorLang') || initialLanguage;
} catch (error) {
    console.warn('Language preference could not be loaded:', error);
}
applyLanguage(initialLanguage);
document.querySelectorAll('#siteLanguage').forEach(select => {
    select.addEventListener('change', () => {
        try {
            localStorage.setItem('viorLang', select.value);
        } catch (error) {
            console.warn('Language preference could not be saved:', error);
        }
        applyLanguage(select.value);
    });
});

const CART_STORAGE_KEY = `viorCart:${document.body.dataset.cartKey || 'guest'}`;
let cart = [];
try {
    const storedCart = JSON.parse(localStorage.getItem(CART_STORAGE_KEY) || '[]');
    cart = Array.isArray(storedCart)
        ? storedCart.filter(item => item && typeof item.name === 'string' && Number.isFinite(Number(item.price)))
        : [];
} catch (error) {
    cart = [];
}

const WHATSAPP_URL = 'https://wa.me/254703360356?text=' + encodeURIComponent('Hello Vior Hotel, I would like to make an inquiry.');
let trackingMap = null;
let destinationMarker = null;
let driverMarker = null;
let deliveryLine = null;
let trackingPoller = null;
let trackedOrderNumber = null;

function setupMenuSearchAndFavorites() {
    const search = document.getElementById('menuSearch');
    const favoritesOnly = document.getElementById('favoritesOnly');
    if (!search || !favoritesOnly) return;
    const status = document.getElementById('menuSearchStatus');
    const cards = [...document.querySelectorAll('.menu-card')];
    let favorites = [];
    try {
        const saved = JSON.parse(localStorage.getItem('viorFavorites') || '[]');
        favorites = Array.isArray(saved) ? saved.filter(name => typeof name === 'string') : [];
    } catch (error) {
        console.error('Saved meals could not be loaded:', error);
    }
    const saveFavorites = () => {
        try {
            localStorage.setItem('viorFavorites', JSON.stringify(favorites));
        } catch (error) {
            console.error('Saved meals could not be stored:', error);
            status.textContent = 'Your browser could not save favorite meals.';
        }
    };
    cards.forEach(card => {
        const name = card.querySelector('.menu-copy h4')?.textContent?.trim();
        if (!name) return;
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'favorite-toggle';
        button.setAttribute('aria-label', `Save ${name} to favorites`);
        button.setAttribute('aria-pressed', String(favorites.includes(name)));
        button.textContent = favorites.includes(name) ? '♥' : '♡';
        button.addEventListener('click', () => {
            favorites = favorites.includes(name)
                ? favorites.filter(item => item !== name)
                : [...favorites, name];
            button.setAttribute('aria-pressed', String(favorites.includes(name)));
            button.setAttribute(
                'aria-label',
                `${favorites.includes(name) ? 'Remove' : 'Save'} ${name} ${favorites.includes(name) ? 'from' : 'to'} favorites`,
            );
            button.textContent = favorites.includes(name) ? '♥' : '♡';
            saveFavorites();
            filterCards();
        });
        card.querySelector('.menu-copy')?.prepend(button);
    });

    function filterCards() {
        const term = search.value.trim().toLocaleLowerCase();
        let visible = 0;
        cards.forEach(card => {
            const name = card.querySelector('.menu-copy h4')?.textContent || '';
            const description = card.querySelector('.menu-copy p')?.textContent || '';
            const matches = `${name} ${description}`.toLocaleLowerCase().includes(term);
            const favorite = favorites.includes(name.trim());
            const show = matches && (!favoritesOnly.checked || favorite);
            card.hidden = !show;
            if (show) visible += 1;
        });
        document.querySelectorAll('.menu-category').forEach(category => {
            category.hidden = !category.querySelector('.menu-card:not([hidden])');
        });
        status.textContent = `${visible} meal${visible === 1 ? '' : 's'} shown`;
    }
    search.addEventListener('input', filterCards);
    favoritesOnly.addEventListener('change', filterCards);
    filterCards();
}

setupMenuSearchAndFavorites();

function addToCart(itemName, itemPrice) {
    let catalog = [];
    try {
        catalog = JSON.parse(document.body.dataset.menu || '[]');
    } catch (error) {
        alert('Menu data could not be loaded. Please refresh the page.');
        return;
    }
    const menuItem = catalog.find(item => item.name === itemName);
    if (!menuItem || Number(menuItem.price) !== Number(itemPrice)) {
        alert('Sorry, this menu item is currently unavailable. Please refresh the page.');
        return;
    }
    cart.push({name: itemName, price: Number(itemPrice)});
    localStorage.setItem(CART_STORAGE_KEY, JSON.stringify(cart));
    alert(`${itemName} added to cart!`);
}

function loadCart() {
    const cartDiv = document.getElementById('cartItems');
    const totalDiv = document.getElementById('cartTotal');
    if (!cartDiv || !totalDiv) return;
    cartDiv.replaceChildren();
    let total = 0;
    if (!cart.length) {
        const empty = document.createElement('p');
        empty.textContent = 'Your cart is empty.';
        cartDiv.appendChild(empty);
    } else {
        cart.forEach((item, index) => {
            const row = document.createElement('div');
            row.className = 'cart-item';
            const name = document.createElement('span');
            name.textContent = item.name;
            const price = document.createElement('span');
            price.textContent = `KES ${Number(item.price)}`;
            const remove = document.createElement('button');
            remove.type = 'button';
            remove.className = 'remove-item';
            remove.textContent = 'Remove';
            remove.addEventListener('click', () => {
                cart.splice(index, 1);
                localStorage.setItem(CART_STORAGE_KEY, JSON.stringify(cart));
                loadCart();
            });
            row.append(name, price, remove);
            cartDiv.appendChild(row);
            total += Number(item.price);
        });
    }
    totalDiv.textContent = `Total: KES ${total}`;
    return total;
}

function setupTrackingMap() {
    const mapContainer = document.getElementById('trackingMap');
    if (!mapContainer || typeof window.L === 'undefined') return null;
    if (trackingMap) return trackingMap;
    trackingMap = window.L.map(mapContainer, {zoomControl: true}).setView([0, 0], 2);
    window.L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
        attribution: '&copy; OpenStreetMap contributors',
        maxZoom: 19,
    }).addTo(trackingMap);
    return trackingMap;
}

function parseUtcTimestamp(value) {
    if (!value) return null;
    const normalized = /(?:Z|[+-]\d{2}:?\d{2})$/.test(value) ? value : `${value}Z`;
    const parsed = new Date(normalized);
    return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function updateTrackingMap(status, data) {
    const map = setupTrackingMap();
    if (!map) return;
    const mapCard = document.getElementById('trackingMapCard');
    if (mapCard) mapCard.hidden = true;
    [destinationMarker, driverMarker, deliveryLine].forEach(layer => layer?.remove());
    destinationMarker = driverMarker = deliveryLine = null;

    const destination = data?.destination;
    if (!destination?.verified || !Number.isFinite(Number(destination.latitude)) || !Number.isFinite(Number(destination.longitude))) return;
    if (mapCard) mapCard.hidden = false;
    window.setTimeout(() => map.invalidateSize(), 0);
    const destinationPoint = [Number(destination.latitude), Number(destination.longitude)];
    destinationMarker = window.L.marker(destinationPoint).addTo(map)
        .bindPopup(`<b>Verified delivery destination</b><br>${destination.postal_code || ''}`);
    const boundsPoints = [destinationPoint];
    const driver = data.driver;
    const driverDate = parseUtcTimestamp(driver?.updated_at);
    const ageSeconds = driverDate ? Math.floor((Date.now() - driverDate.getTime()) / 1000) : Infinity;
    const fresh = ageSeconds >= 0 && ageSeconds < 120;
    const ageBadge = document.getElementById('driverLocationAge');
    if (ageBadge) ageBadge.textContent = fresh ? `GPS · ${ageSeconds}s ago` : driver ? 'GPS is stale' : 'No live signal';

    if (fresh && status === 'Out for Delivery' && Number.isFinite(Number(driver.latitude)) && Number.isFinite(Number(driver.longitude))) {
        const driverPoint = [Number(driver.latitude), Number(driver.longitude)];
        driverMarker = window.L.marker(driverPoint, {
            icon: window.L.divIcon({className: 'driver-marker', html: '<div class="driver-pin"></div>', iconSize: [22, 22], iconAnchor: [11, 11]}),
        }).addTo(map).bindPopup('<b>Driver live GPS</b>');
        deliveryLine = window.L.polyline([driverPoint, destinationPoint], {
            color: '#355e50', weight: 4, opacity: 0.75, dashArray: '8 8',
        }).addTo(map);
        boundsPoints.push(driverPoint);
    }
    const eta = document.getElementById('trackingEta');
    if (eta) eta.textContent = fresh && status === 'Out for Delivery'
        ? 'Live driver position shown · ETA not available'
        : 'ETA appears when the hotel provides one';
    map.fitBounds(boundsPoints, {padding: [32, 32], maxZoom: 15});
}

function trackOrderStatus(orderNumber, autoRefresh = false) {
    const result = document.getElementById('trackingResult');
    const statusLabel = document.getElementById('trackingStatusLabel');
    const progress = document.getElementById('trackingProgress');
    if (!result || !statusLabel || !progress) return;
    trackedOrderNumber = orderNumber;
    result.hidden = false;
    statusLabel.textContent = 'Checking order…';
    progress.style.width = '0%';

    const fetchStatus = async () => {
        try {
            const response = await fetch(`/api/order/status/${encodeURIComponent(orderNumber)}`);
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Order not found.');
            const status = data.status || 'Pending';
            const rawStatus = (data.raw_status || status).toLowerCase();
            const percentages = {Pending: 20, Preparing: 50, 'Out for Delivery': 78, Delivered: 100};
            const messages = {
                Pending: rawStatus.includes('till') ? 'Waiting for the hotel to confirm your Till payment.' : rawStatus.includes('verified') ? 'Payment confirmed. The hotel will update this order when preparation begins.' : rawStatus.includes('payment') ? 'Waiting for payment confirmation.' : 'Your order has been received. The hotel will update it when preparation begins.',
                Preparing: 'The kitchen is preparing your order.',
                'Out for Delivery': 'Your order has been dispatched. Live driver GPS appears here when the driver shares their location.',
                Delivered: 'Your order has been marked as delivered. Thank you for ordering with Vior Hotel.',
            };
            statusLabel.textContent = status;
            document.getElementById('trackingMessage').textContent = messages[status] || data.raw_status || status;
            progress.style.width = `${percentages[status] || 15}%`;
            progress.parentElement.setAttribute('aria-valuenow', String(percentages[status] || 15));
            const currentStep = {Pending: 0, Preparing: 1, 'Out for Delivery': 2, Delivered: 3}[status] ?? 0;
            document.querySelectorAll('#trackingSteps li').forEach((step, index) => {
                step.classList.toggle('is-complete', index < currentStep);
                step.classList.toggle('is-current', index === currentStep);
            });
            const badge = document.getElementById('mapStatusBadge');
            if (badge) badge.textContent = status === 'Out for Delivery' ? 'Dispatched' : status;
            const updatedAt = parseUtcTimestamp(data.status_updated_at);
            const updatedLabel = document.getElementById('trackingUpdatedAt');
            if (updatedLabel) updatedLabel.textContent = updatedAt ? `Last update: ${updatedAt.toLocaleString()}` : 'Last update: Awaiting first staff update';
            updateTrackingMap(status, data);
            const refreshLabel = document.getElementById('trackingRefreshState');
            if (refreshLabel) refreshLabel.textContent = status === 'Delivered' ? 'Tracking complete' : 'Updates every 10 seconds';
            clearInterval(trackingPoller);
            if (autoRefresh && status !== 'Delivered') trackingPoller = setInterval(fetchStatus, 10000);
        } catch (error) {
            statusLabel.textContent = 'Order not found';
            document.getElementById('trackingMessage').textContent = error.message || 'Check the order number and try again.';
            progress.style.width = '0%';
            clearInterval(trackingPoller);
            document.getElementById('trackingRefreshState').textContent = 'Check the reference and try again.';
        }
    };
    clearInterval(trackingPoller);
    fetchStatus();
}

document.getElementById('trackOrderForm')?.addEventListener('submit', event => {
    event.preventDefault();
    const number = document.getElementById('trackOrderNumber').value.trim();
    if (number) trackOrderStatus(number, true);
});

document.addEventListener('visibilitychange', () => {
    if (!document.hidden && trackedOrderNumber) trackOrderStatus(trackedOrderNumber, true);
});

document.getElementById('reviewForm')?.addEventListener('submit', function(event) {
    event.preventDefault();
    const name = document.getElementById('reviewName').value.trim();
    const text = document.getElementById('reviewText').value.trim();
    if (!name || !text) return;
    fetch('/api/review', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name, text}),
    }).then(async response => {
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || 'Could not submit your review.');
        const review = document.createElement('p');
        const quote = document.createElement('span');
        quote.textContent = `“${text}”`;
        const author = document.createElement('strong');
        author.textContent = ` — ${name}`;
        review.append(quote, author);
        document.getElementById('reviewList').prepend(review);
        alert('Thank you! Your review has been submitted.');
        this.reset();
    }).catch(error => alert(error.message || 'Network error. Please try again.'));
});

function triggerRippleEffect(event) {
    const button = event.currentTarget;
    if (!button || button.classList.contains('ripple')) return;
    button.classList.add('ripple');
    window.setTimeout(() => button.classList.remove('ripple'), 550);
}

document.querySelectorAll('.btn').forEach(button => {
    button.addEventListener('click', triggerRippleEffect);
});

function generateInvoice(orderDetails) {
    const {jsPDF} = window.jspdf;
    const doc = new jsPDF();
    doc.setFontSize(22);
    doc.text('Vior Hotel Invoice', 20, 20);
    doc.setFontSize(12);
    doc.text(`Order Number: ${orderDetails.orderNumber}`, 20, 40);
    doc.text(`Date: ${orderDetails.date}`, 20, 50);
    doc.text(`Time: ${orderDetails.time}`, 20, 60);
    doc.text(`Order Type: ${orderDetails.type}`, 20, 70);
    doc.text(`Delivery Time: ${orderDetails.deliveryTime}`, 20, 80);
    doc.text(`Total Amount: KES ${orderDetails.amount}`, 20, 90);
    doc.save(`Vior_Invoice_${orderDetails.orderNumber}.pdf`);
}

function sendChatMessage() {
    const input = document.getElementById('chatInput');
    if (!input) return;
    const text = input.value.trim();
    if (!text) return;
    const chatBox = document.getElementById('chatBox');
    const message = document.createElement('p');
    const sender = document.createElement('strong');
    sender.textContent = 'You: ';
    message.append(sender, document.createTextNode(text));
    chatBox.appendChild(message);
    input.value = '';
    window.setTimeout(() => {
        const reply = document.createElement('p');
        const support = document.createElement('strong');
        support.textContent = 'Vior Hotel Support: ';
        reply.append(support, document.createTextNode('Thank you for your message. We will get back to you shortly.'));
        chatBox.appendChild(reply);
        chatBox.scrollTop = chatBox.scrollHeight;
    }, 1000);
}