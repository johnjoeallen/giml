package org.giml.fixture.app;

public final class Greeter {
    public String greet(String name) {
        return name == null ? "Hello" : "Hello, " + name;
    }
}
