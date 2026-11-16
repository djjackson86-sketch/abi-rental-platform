  const moneySymbol={{ config.CURRENCY_SYMBOL|tojson }};
  const moneyNumber=new Intl.NumberFormat('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
  function fmt(value){
    let n=Number(value||0);
    if(!Number.isFinite(n)) n=0;
    const amount=moneyNumber.format(Math.abs(n)).replace(/,/g,' ');
    return (n<0 && amount!=='0.00'?'-':'')+moneySymbol+amount;
  }
