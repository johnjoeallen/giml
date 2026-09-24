package org.giml.fixture.core;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class CalculatorTest {
    private final Calculator calculator = new Calculator();

    @Test
    void adds() {
        assertEquals(5, calculator.add(2, 3));
    }

    @Test
    void divides() {
        assertEquals(2, calculator.divide(7, 3));
    }
}
