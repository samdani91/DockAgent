const express = require('express');

const app = express();
const PORT = process.env.PORT || 3000;

app.get('/health', (req, res) => res.json({ status: 'ok' }));
app.get('/', (req, res) => res.send('express-undocumented'));

app.listen(PORT, () => console.log(`listening on ${PORT}`));
