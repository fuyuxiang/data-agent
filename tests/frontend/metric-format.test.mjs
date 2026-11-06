import assert from 'node:assert/strict';
import test from 'node:test';
import { metricFormatter } from '../../frontend/src/metric-format.js';

test('decimal strings keep cents and round in decimal, including negative values', () => {
  const money = metricFormatter(',.2f');
  assert.equal(money('9999999999999999.99'), '9,999,999,999,999,999.99');
  assert.equal(money('-9999999999999999.995'), '-10,000,000,000,000,000.00');
  assert.equal(money('1.005'), '1.01');
  assert.equal(money('-1.005'), '-1.01');
  assert.equal(money('1.234567890123456789e20'), '123,456,789,012,345,678,900.00');
});

test('percentage, integer, zero and high precision formats follow the definition', () => {
  const percent = metricFormatter('.2%');
  assert.equal(percent('0.07591737805036294'), '7.59%');
  assert.equal(percent('-0.125'), '-12.50%');
  assert.equal(percent(0), '0.00%');
  assert.equal(metricFormatter(',.0f')('9007199254740993'), '9,007,199,254,740,993');
  assert.equal(metricFormatter('.20f')('0.12345678901234567890'), '0.12345678901234567890');
});

test('missing values and unsupported formats preserve their meaning', () => {
  for (const value of [null, undefined, '', NaN]) assert.equal(metricFormatter('.2f')(value), '—');
  for (const value of ['not a number', ' ', '1e999', false]) {
    assert.equal(metricFormatter('.2f')(value), String(value));
  }
  assert.equal(metricFormatter('')('123.4567890123456789'), '123.4567890123456789');
  assert.equal(metricFormatter('unsupported')('123.4567890123456789'), '123.4567890123456789');
  assert.equal(metricFormatter('', value => `raw:${value}`)(12), 'raw:12');
});
