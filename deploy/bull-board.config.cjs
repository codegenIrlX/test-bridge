const fs = require('node:fs');

const svg = fs.readFileSync('/app/test-bridge-mark.svg');

module.exports = {
  uiConfig: {
    boardTitle: 'Test Bridge',
    boardLogo: {
      path: `data:image/svg+xml;base64,${svg.toString('base64')}`,
      width: 36,
      height: 36,
    },
  },
};
