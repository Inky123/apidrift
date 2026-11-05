<?php

namespace App;

class Merged
{
    // the key is visible, but the array is merged with one that is not: the fixer leaves it for a person
    public function charge(array $extra, string $note)
    {
        return \Stripe\Charge::create(array_merge(['description' => $note], $extra));
    }
}
