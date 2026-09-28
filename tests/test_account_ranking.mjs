import { test } from 'node:test';
import assert from 'node:assert/strict';
import { sortAccounts } from '../app/static/js/account-ranking.js';

test('account ranking keeps real zero, missing metrics and stable ties distinct', () => {
  const rows = [
    {account:'zeta', engagement:null}, {account:'beta', engagement:12},
    {account:'zero', engagement:0}, {account:'alpha', engagement:12},
    {account:'unknown', engagement:undefined}, {account:'invalid', engagement:'bad'},
  ];
  const names = order => sortAccounts(rows, order).map(row => row.account);
  assert.deepEqual(names('desc'), ['alpha','beta','zero','invalid','unknown','zeta']);
  assert.deepEqual(names('asc'), ['zero','alpha','beta','invalid','unknown','zeta']);
  assert.deepEqual(names('name'), ['alpha','beta','invalid','unknown','zero','zeta']);
  assert.equal(rows[0].account, 'zeta');
});
